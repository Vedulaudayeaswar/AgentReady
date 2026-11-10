"""Convert AI output into canonical products — with independent verification.

An LLM's own claims are not evidence. For every AI-provided field we check
that the quoted ``source_text`` (or the value itself) literally occurs in the
content we sent. Unverifiable prices, currencies, availability, URLs and
images are dropped; other unverifiable fields are kept but marked
``verified_in_source=False`` and flagged for review.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.schemas.canonical import (
    ExtractedProduct,
    ProductEvidence,
    ProductImage,
    ProductPrice,
    ProductVariant,
)
from app.services.ai.base import AIProduct
from app.services.ai.prompts import EXTRACTION_PROMPT_VERSION
from app.services.normalization.normalize import (
    clean_text,
    detect_currency,
    make_attribute,
    normalize_availability,
    normalize_currency_code,
    normalize_gtin,
    parse_measurement,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class SourceIndex:
    def __init__(self, content: str):
        self.text = _norm(content)

    def contains(self, snippet: str | None) -> bool:
        if not snippet:
            return False
        s = _norm(str(snippet))
        return len(s) >= 1 and s in self.text

    def has_amount(self, amount: float) -> bool:
        d = Decimal(str(amount)).normalize()
        forms = {format(d, "f"), f"{d:.2f}", f"{int(d)}" if d == int(d) else format(d, "f")}
        flat = self.text.replace(",", "")
        return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", flat) for f in forms)


def ai_to_canonical(
    item: AIProduct,
    content: str,
    page_url: str,
    allowed_images: list[str],
    model_id: str,
    method: str,
    source_type: str = "ai",
) -> ExtractedProduct:
    idx = SourceIndex(content)
    p = ExtractedProduct(origin=method, source_url=page_url)
    quotes: dict[str, str] = {}
    for ev in item.evidence:
        quotes.setdefault(ev.field.split(".")[0].lower(), ev.source_text)

    def ev(field: str, value, quote: str | None, verified: bool, inferred: bool = False) -> None:
        p.evidence.append(
            ProductEvidence(
                field=field, value=value, source_value=(quote or "")[:500] or None, source_url=page_url,
                source_fragment=(quote or "")[:1500] or None, source_type=source_type,  # type: ignore[arg-type]
                extraction_method=method,  # type: ignore[arg-type]
                inferred=inferred, model_id=model_id, prompt_version=EXTRACTION_PROMPT_VERSION,
                confidence=0.6 if verified else 0.3, verified_in_source=verified,
            )
        )
        if not verified:
            p.flags.append({"code": "unverified_ai_evidence", "field": field,
                            "message": f"AI-provided '{field}' could not be found verbatim in the source content"})

    def simple(field: str, value: str | None, max_len: int = 500, inferred: bool = False) -> None:
        v = clean_text(value, max_len)
        if not v:
            return
        q = quotes.get(field)
        verified = idx.contains(q) or idx.contains(v)
        setattr(p, field, v)
        ev(field, v, q or v, verified, inferred)

    simple("name", item.name)
    simple("sku", item.sku, 200)
    simple("brand", item.brand, 200)
    simple("manufacturer", item.manufacturer, 200)
    simple("description", item.description, 5000)
    simple("mpn", item.mpn, 100)
    simple("material", item.material, 200)
    if item.category:
        simple("category", item.category, 300, inferred=item.category_is_inferred)
        p.category_inferred = item.category_is_inferred
    if item.gtin:
        g = normalize_gtin(item.gtin)
        if g and idx.contains(g):
            p.gtin = g
            ev("gtin", g, quotes.get("gtin") or g, True)
        else:
            p.flags.append({"code": "ai_value_dropped", "field": "gtin", "message": "AI GTIN not present in source; dropped"})

    # ---- price: drop unless the amount literally appears in the source
    if item.price and item.price.amount is not None:
        q = item.price.source_text
        if idx.has_amount(item.price.amount):
            cur = normalize_currency_code(item.price.currency)
            cur_inferred = False
            if cur:
                detected, amb = detect_currency(q or "")
                if detected == cur:
                    cur_inferred = amb
                elif not idx.contains(cur):
                    p.flags.append({"code": "ai_value_dropped", "field": "currency",
                                    "message": f"AI currency '{cur}' not supported by source text; dropped"})
                    cur = None
            price = ProductPrice(amount=Decimal(str(item.price.amount)).quantize(Decimal("0.01")), currency=cur,
                                 raw=q, currency_inferred=cur_inferred)
            p.price = price
            ev("price", price.model_dump(mode="json"), q, idx.contains(q) if q else True)
        else:
            p.flags.append({"code": "ai_value_dropped", "field": "price",
                            "message": "AI price not present in source content; dropped (prices are never invented)"})

    # ---- availability: explicit phrase required
    if item.availability:
        q = item.availability_source_text
        av = normalize_availability(item.availability)
        if av and q and idx.contains(q) and normalize_availability(q):
            p.availability = av
            ev("availability", av, q, True)
        else:
            p.flags.append({"code": "ai_value_dropped", "field": "availability",
                            "message": "Availability not explicitly stated in source; left empty"})

    if item.product_url:
        if idx.contains(item.product_url) or item.product_url.rstrip("/") == page_url.rstrip("/"):
            p.product_url = item.product_url
            ev("product_url", item.product_url, item.product_url, True)

    allowed = set(allowed_images)
    p.images = [ProductImage(url=u) for u in item.image_urls if u in allowed][:10]
    if p.images:
        ev("images", [i.url for i in p.images], ", ".join(i.url for i in p.images), True)

    for fld in ("dimensions", "weight"):
        raw = getattr(item, fld)
        if raw:
            m = parse_measurement(raw)
            verified = idx.contains(raw)
            setattr(p, fld, m)
            ev(fld, m.model_dump() if m else None, raw, verified)

    for a in item.attributes:
        raw = f"{a.value or ''}{(' ' + a.unit) if a.unit and a.value and a.unit not in a.value else ''}".strip()
        attr = make_attribute(a.name, raw or None)
        if not attr:
            continue
        if a.unit and not attr.unit:
            attr.unit = a.unit
        verified = idx.contains(a.source_text) or idx.contains(a.value)
        p.attributes.append(attr)
        ev(f"attributes.{attr.key}", attr.value, a.source_text or a.value, verified)

    for v in item.variants:
        price = None
        if v.price_amount is not None and idx.has_amount(v.price_amount):
            price = ProductPrice(amount=Decimal(str(v.price_amount)).quantize(Decimal("0.01")),
                                 currency=normalize_currency_code(v.currency) if v.currency and idx.contains(v.currency) else None)
        p.variants.append(ProductVariant(name=clean_text(v.name), sku=clean_text(v.sku), price=price,
                                         attributes={kv.name: kv.value or "" for kv in v.attributes}))

    for f in item.ambiguous_fields:
        p.flags.append({"code": "ambiguous_field", "field": f, "message": f"The AI marked '{f}' as ambiguous"})
    return p
