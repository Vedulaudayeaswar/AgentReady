"""Per-page extraction: deterministic first, AI only where it adds value."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bs4 import BeautifulSoup

from app.core.config import Settings, get_settings
from app.schemas.canonical import ExtractedProduct, ExtractionResult
from app.services.ai.convert import ai_to_canonical
from app.services.ai.gateway import AIGateway
from app.services.discovery.classifier import Classification, classify_page
from app.services.extraction.content import html_to_text, prepare_page_content, prepare_table_content
from app.services.extraction.html import heuristic_product, main_region, product_cards
from app.services.extraction.limits import check_page_complexity
from app.services.extraction.structured import map_jsonld_product, meta_product, parse_jsonld_blocks, parse_meta, parse_microdata
from app.services.extraction.tables import iter_tables, products_from_table, spec_attributes
from app.services.normalization.normalize import normalize_name_for_match

log = logging.getLogger("agentready.extraction")

SCALAR_FIELDS = ("name", "sku", "brand", "manufacturer", "category", "description", "availability",
                 "product_url", "gtin", "mpn", "material", "dimensions", "weight")


@dataclass
class PageOutcome:
    classification: Classification
    result: ExtractionResult
    text_excerpt: str
    title: str | None


def _values_conflict(field: str, a, b) -> bool:
    if a is None or b is None:
        return False
    if field == "name":
        na, nb = normalize_name_for_match(a), normalize_name_for_match(b)
        return not (na in nb or nb in na)
    if field == "description":
        return False
    if field in ("dimensions", "weight"):
        return a.value != b.value
    return str(a).strip().lower() != str(b).strip().lower()


def merge_sources(sources: list[ExtractedProduct]) -> ExtractedProduct:
    """Merge records describing the SAME product, ordered from most to least trusted."""
    sources = [s for s in sources if s is not None]
    base = sources[0].model_copy(deep=True)
    for other in sources[1:]:
        for f in SCALAR_FIELDS:
            mine, theirs = getattr(base, f), getattr(other, f)
            if mine in (None, "") and theirs not in (None, ""):
                setattr(base, f, theirs)
                base.evidence.extend(other.evidence_for(f))
            elif _values_conflict(f, mine, theirs):
                base.flags.append({"code": "conflicting_values", "field": f,
                                   "message": f"{base.origin} and {other.origin} disagree on {f}",
                                   "values": [str(mine)[:200], str(theirs)[:200]]})
                base.evidence.extend(other.evidence_for(f))
        if other.price and other.price.amount is not None:
            if base.price is None or base.price.amount is None:
                base.price = other.price
                base.evidence.extend(other.evidence_for("price"))
            elif base.price.amount != other.price.amount:
                base.flags.append({"code": "conflicting_values", "field": "price",
                                   "message": f"{base.origin} and {other.origin} report different prices",
                                   "values": [str(base.price.amount), str(other.price.amount)]})
                base.evidence.extend(other.evidence_for("price"))
            elif base.price.currency is None and other.price.currency:
                base.price.currency = other.price.currency
                base.price.currency_inferred = other.price.currency_inferred
        keys = {a.key for a in base.attributes}
        for a in other.attributes:
            if a.key not in keys:
                base.attributes.append(a)
                keys.add(a.key)
                base.evidence.extend(other.evidence_for(f"attributes.{a.key}"))
        urls = {i.url for i in base.images}
        for img in other.images:
            if img.url not in urls:
                base.images.append(img)
                urls.add(img.url)
        if not base.variants and other.variants:
            base.variants = other.variants
        base.flags.extend(fl for fl in other.flags if fl.get("code") != "conflicting_values")
        if other.category_inferred and base.category == other.category:
            base.category_inferred = True
    return base


class PageExtractor:
    def __init__(self, gateway: AIGateway | None = None, settings: Settings | None = None, methods: list[str] | None = None):
        self.ai = gateway
        self.s = settings or get_settings()
        # enabled extraction methods (per-website sync settings): structured_data, tables, html, ai
        self.methods = set(methods) if methods else {"structured_data", "tables", "html", "ai"}

    def _ai_ok(self) -> bool:
        return (self.ai is not None and self.ai.available and self.s.ai_extraction_mode != "never"
                and "ai" in self.methods)

    async def _ai_products(self, content: str, page_url: str, images: list[str], source_type: str = "ai") -> list[ExtractedProduct]:
        assert self.ai is not None
        resp = await self.ai.extract(content, page_url)
        if resp is None or not resp.products:
            return []
        out = []
        for item in resp.products:
            p = ai_to_canonical(item, content, page_url, images, self.ai.model_id, self.ai.method, source_type)
            if p.name:
                out.append(p)
        return out

    async def extract(self, url: str, html: str) -> PageOutcome:
        check_page_complexity(html)  # raises PageTooComplex for pathological pages
        soup = BeautifulSoup(html, "lxml")
        meta = parse_meta(soup)
        title = soup.title.get_text(strip=True)[:500] if soup.title else None
        cls = classify_page(url, soup, meta)
        excerpt = html_to_text(main_region(soup))[:20000]
        result = ExtractionResult(page_url=url, page_type=cls.page_type)

        if cls.ambiguous and self._ai_ok():
            content, _ = prepare_page_content(soup, url, min(4000, self.s.max_ai_content_chars))
            verdict = await self.ai.classify(content, url)  # type: ignore[union-attr]
            if verdict is not None:
                cls.reasons.append(f"AI classification ({self.ai.model_id}): {verdict.reason or ''}".strip())  # type: ignore[union-attr]
                if verdict.is_listing_page:
                    cls.page_type = "listing"
                elif verdict.is_product_page:
                    cls.page_type = "product"
                result.page_type = cls.page_type
                result.used_ai = True

        if cls.page_type == "other":
            return PageOutcome(cls, result, excerpt, title)

        structured = "structured_data" in self.methods
        jsonld = [map_jsonld_product(n, url) for n in parse_jsonld_blocks(soup)] if structured else []
        micro = parse_microdata(soup, url) if structured else []
        tables = iter_tables(soup) if "tables" in self.methods else []
        table_products: list[ExtractedProduct] = []
        spec_attrs, spec_ev = [], []
        ambiguous_tables = []
        for table, analysis in tables:
            if analysis.kind == "product":
                table_products.extend(products_from_table(analysis, table, url))
            elif analysis.kind == "spec":
                a, e = spec_attributes(analysis, url)
                spec_attrs.extend(a)
                spec_ev.extend(e)
            elif analysis.kind == "ambiguous":
                ambiguous_tables.append(analysis)

        products: list[ExtractedProduct] = []
        is_single = cls.page_type == "product" and len(jsonld) <= 1 and len(micro) <= 1 and len(table_products) <= 1

        if is_single:
            heur = heuristic_product(soup, url, meta) if "html" in self.methods else None
            mp = meta_product(meta, url) if structured else None
            ordered = [*(jsonld[:1]), *(micro[:1]), *(table_products[:1]), mp, heur]
            ordered = [o for o in ordered if o is not None]
            if ordered:
                merged = merge_sources(ordered)
                keys = {a.key for a in merged.attributes}
                for a in spec_attrs:
                    if a.key not in keys:
                        merged.attributes.append(a)
                        keys.add(a.key)
                merged.evidence.extend(spec_ev)
                merged.product_url = merged.product_url or url
                products = [merged]
            need_ai = self.s.ai_extraction_mode == "always" or not products or products[0].name is None or products[0].price is None
            if need_ai and self._ai_ok():
                content, images = prepare_page_content(soup, url, self.s.max_ai_content_chars)
                ai = await self._ai_products(content, url, images)
                result.used_ai = result.used_ai or bool(ai) or self.ai.calls > 0  # type: ignore[union-attr]
                if ai:
                    products = [merge_sources([products[0], ai[0]])] if products else [ai[0]]
                    if not products[0].product_url:
                        products[0].product_url = url
        else:
            products = [*jsonld, *micro, *table_products]
            if not products and "html" in self.methods:
                products = product_cards(soup, url)
            need_ai = self.s.ai_extraction_mode == "always" or not products
            if need_ai and self._ai_ok():
                content, images = prepare_page_content(soup, url, self.s.max_ai_content_chars)
                ai = await self._ai_products(content, url, images)
                result.used_ai = True
                if not products:
                    products = ai
            # ambiguous tables -> send only the table text to the AI (much cheaper than the page)
            for analysis in ambiguous_tables:
                if not self._ai_ok():
                    result.notes.append("ambiguous table skipped: AI unavailable")
                    continue
                heading = soup.find(["h1", "h2"])
                content = prepare_table_content(analysis.text, heading.get_text(" ", strip=True) if heading else None,
                                                self.s.max_ai_content_chars)
                ai = await self._ai_products(content, url, [], source_type="ai")
                result.used_ai = True
                products.extend(ai)

        for p in products:
            p.source_url = url
        result.products = [p for p in products if p.name]
        if result.used_ai and self.ai:
            result.ai_model = self.ai.model_id
        return PageOutcome(cls, result, excerpt, title)
