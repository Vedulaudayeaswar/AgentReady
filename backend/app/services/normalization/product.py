"""Final normalization of an extracted product into catalog-ready form."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app.schemas.canonical import ExtractedProduct, ProductEvidence
from app.services.crawler.fetcher import ip_is_public
from app.services.crawler.urls import normalize_url
from app.services.normalization.normalize import clean_text, normalize_currency_code, normalize_name_for_match


def _http_url(u: str | None) -> str | None:
    if not u:
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        if not ip_is_public(ipaddress.ip_address(host)):
            return None  # e.g. an <img> pointing at a cloud-metadata or LAN address
    except ValueError:
        pass  # a hostname, not an IP literal
    try:
        return normalize_url(u)
    except ValueError:
        return None


def has_own_page(p: ExtractedProduct) -> bool:
    """Rows/cards on a listing share the page URL; only a link to a separate page identifies them."""
    if not p.product_url:
        return False
    return p.origin not in ("html_table", "html_card") or p.product_url != p.source_url


def match_key(p: ExtractedProduct) -> str:
    """Stable identity used to match products across scans and within a scan."""
    if p.sku:
        return f"sku:{p.sku.strip().lower()}"
    if p.gtin:
        return f"gtin:{p.gtin}"
    if has_own_page(p):
        return f"url:{p.product_url}"
    return f"name:{normalize_name_for_match(p.name)}"[:512]


def finalize(p: ExtractedProduct, default_currency: str | None = None) -> ExtractedProduct:
    p.name = clean_text(p.name, 500)
    p.brand = clean_text(p.brand, 200)
    p.sku = clean_text(p.sku, 200)
    p.category = clean_text(p.category, 300)
    p.product_url = _http_url(p.product_url)
    p.source_url = _http_url(p.source_url)
    seen, images = set(), []
    for img in p.images:
        u = _http_url(img.url)
        if u and u not in seen:
            seen.add(u)
            images.append(img.model_copy(update={"url": u}))
    p.images = images[:20]
    if p.price:
        p.price.currency = normalize_currency_code(p.price.currency)
        if p.price.amount is not None and not p.price.currency and default_currency:
            p.price.currency = default_currency.upper()
            p.price.currency_inferred = True
            p.evidence.append(
                ProductEvidence(
                    field="currency", value=p.price.currency, source_value="Business default currency setting",
                    source_url=p.source_url, source_type="merchant_edit", extraction_method="merchant",
                    inferred=True, confidence=None, verified_in_source=False,
                )
            )
            p.flags.append({"code": "currency_from_default", "field": "currency",
                            "message": "No currency on the page; the business default currency was applied"})
    # canonical attribute names (e.g. product_weight -> weight); the original label and raw value are kept
    from app.services.commerce.catalog_rules import BUILTIN_ALIASES

    for a in p.attributes:
        canonical = BUILTIN_ALIASES.get(a.key)
        if canonical and canonical != a.key:
            for ev in p.evidence:
                if ev.field == f"attributes.{a.key}":
                    ev.field = f"attributes.{canonical}"
            a.key = canonical
        if a.key in ("brand", "manufacturer", "material", "mpn") and a.value and not getattr(p, a.key):
            setattr(p, a.key, clean_text(a.value, 200))
            p.evidence.extend([ev.model_copy(update={"field": a.key}) for ev in p.evidence if ev.field == f"attributes.{a.key}"])
    # de-duplicate attributes by key (keep first = most trusted)
    seen_keys, attrs = set(), []
    for a in p.attributes:
        if a.key not in seen_keys:
            seen_keys.add(a.key)
            attrs.append(a)
    p.attributes = attrs[:100]
    return p


def dedupe_within_scan(products: list[ExtractedProduct]) -> list[ExtractedProduct]:
    """Merge records of the same product found on several pages of one scan.

    Prefers the record from a dedicated product page (richer) and fills gaps
    from listing/table records.
    """
    from app.services.extraction.pipeline import merge_sources

    rank = {"json_ld": 0, "microdata": 1, "html": 2, "meta": 3, "html_table": 4, "html_card": 5, "ai": 6, "ai_mock": 6}
    ordered = sorted(products, key=lambda x: rank.get(x.origin, 9))
    # pass 1: the most trusted record for each product URL defines that URL's key
    url_index: dict[str, str] = {}
    for p in ordered:
        if has_own_page(p):
            url_index.setdefault(p.product_url, match_key(p))  # type: ignore[arg-type]
    groups: dict[str, list[ExtractedProduct]] = {}
    for p in ordered:
        k = url_index.get(p.product_url, match_key(p)) if has_own_page(p) else match_key(p)  # type: ignore[arg-type]
        groups.setdefault(k, []).append(p)
    out = []
    for items in groups.values():
        out.append(merge_sources(items) if len(items) > 1 else items[0])
    return out
