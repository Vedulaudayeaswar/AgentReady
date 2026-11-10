"""Heuristic extraction from ordinary HTML (pages without structured data)."""

from __future__ import annotations

import copy
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.schemas.canonical import ExtractedProduct, ProductEvidence, ProductImage
from app.services.normalization.normalize import (
    clean_text,
    find_price_strings,
    make_attribute,
    normalize_availability,
    normalize_gtin,
    parse_measurement,
    parse_price,
)

PRICE_CLASS = re.compile(r"(^|[-_\s])(price|amount|cost|mrp|selling)([-_\s]|$)", re.I)
OLD_PRICE_CLASS = re.compile(r"(old|was|strike|compare|regular|list|original|before|crossed)", re.I)
STOCK_CLASS = re.compile(r"(stock|availability|inventory)", re.I)
DESC_CLASS = re.compile(r"(description|product[-_]?details|overview|summary)", re.I)
LABELLED = {
    "sku": re.compile(r"\b(?:SKU|Item\s*code|Product\s*code|Part\s*(?:No\.?|Number|#)|Model\s*(?:No\.?|Number)|Article\s*No\.?)\s*[:#\-]\s*([A-Za-z0-9][A-Za-z0-9\-_./]{1,40})", re.I),
    "brand": re.compile(r"\bBrand\s*[:\-]\s*([^\n|•,;]{2,60})", re.I),
    "manufacturer": re.compile(r"\bManufacturer\s*[:\-]\s*([^\n|•,;]{2,80})", re.I),
    "gtin": re.compile(r"\b(?:EAN|UPC|GTIN|Barcode)(?:-?13|-?12)?\s*[:#\-]?\s*(\d{8,14})\b", re.I),
    "mpn": re.compile(r"\bMPN\s*[:#\-]\s*([A-Za-z0-9\-_./]{2,40})", re.I),
    "material": re.compile(r"\bMaterial\s*[:\-]\s*([^\n|•,;]{2,60})", re.I),
    "weight": re.compile(r"\b(?:Net\s*)?Weight\s*[:\-]\s*([\d.,]+\s*(?:kg|g|gm|lb|lbs|oz|mg))", re.I),
    "dimensions": re.compile(r"\bDimensions?\s*[:\-]\s*([\d.,]+\s*[x×*]\s*[\d.,]+(?:\s*[x×*]\s*[\d.,]+)?\s*(?:mm|cm|m|in|inch|inches)?)", re.I),
}
ADD_TO_CART = re.compile(r"(add\s+to\s+(cart|bag|basket)|buy\s+now|order\s+now|request\s+(a\s+)?quote|enquire\s+now)", re.I)


def _ev(field, value, raw, url, fragment, conf=0.6) -> ProductEvidence:
    return ProductEvidence(
        field=field, value=value, source_value=(raw or "")[:500], source_url=url,
        source_fragment=(fragment or "")[:1500] or None, source_type="html",
        extraction_method="deterministic", confidence=conf, verified_in_source=True,
    )


def main_region(soup: BeautifulSoup) -> Tag:
    for sel in ("[itemtype*='schema.org/Product']", "main", "article", "[role=main]", "#content", ".product", "#product"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 40:
            return el
    return soup.body or soup


def _is_old_price(el: Tag) -> bool:
    for node in [el, *el.parents][:4]:
        if not isinstance(node, Tag):
            continue
        if node.name in ("del", "s", "strike"):
            return True
        cls = " ".join(node.get("class", [])) + " " + (node.get("id") or "")
        if OLD_PRICE_CLASS.search(cls):
            return True
    return False


def find_price(region: Tag) -> tuple[str | None, str | None]:
    """Return (raw price string, html fragment)."""
    for el in region.find_all(True, attrs={"class": PRICE_CLASS}):
        if _is_old_price(el):
            continue
        # drop struck-through / "was" prices nested inside a price wrapper
        current = copy.copy(el)
        for old in current.find_all(True):
            if old.decomposed:
                continue
            if old.name in ("del", "s", "strike") or OLD_PRICE_CLASS.search(" ".join(old.get("class", []))):
                old.decompose()
        txt = clean_text(current.get_text(" ", strip=True)) or ""
        hits = find_price_strings(txt)
        if hits:
            return hits[0], str(el)[:500]
        if re.fullmatch(r"\d[\d,.]*", txt):
            return txt, str(el)[:500]
    for el in region.find_all(string=True):
        parent = el.parent
        if parent is None or parent.name in ("script", "style", "del", "s") or _is_old_price(parent):
            continue
        hits = find_price_strings(str(el))
        if hits:
            return hits[0], str(parent)[:500]
    return None, None


PRODUCT_SCOPE = re.compile(r"schema\.org/(Product|ProductModel|IndividualProduct)", re.I)


def _product_heading(soup: BeautifulSoup) -> Tag | None:
    """The element the page itself marks as the product name (itemprop="name" inside a Product scope).

    Preferred over <h1>, which on many shops is a site banner ("Test Sites", "Our Store") rather than the
    product name.
    """
    scope = soup.find(attrs={"itemscope": True, "itemtype": PRODUCT_SCOPE})
    if scope is None:
        return None
    for el in scope.find_all(attrs={"itemprop": True}):
        if "name" in el.get("itemprop", "").split() and el.find_parent(attrs={"itemscope": True}) is scope:
            return el if clean_text(el.get_text(" ", strip=True)) else None
    return None


def heuristic_product(soup: BeautifulSoup, page_url: str, meta: dict[str, str]) -> ExtractedProduct | None:
    region = main_region(soup)
    p = ExtractedProduct(origin="html", source_url=page_url, product_url=page_url)
    h1 = _product_heading(soup) or region.find("h1") or soup.find("h1")
    name = clean_text(h1.get_text(" ", strip=True), 500) if h1 else None
    if name and (len(name) > 150 or find_price_strings(name)):
        # malformed markup (e.g. unclosed <h1>) swallowed following content: keep the heading's own text
        first = h1.find(string=True)
        name = clean_text(str(first), 300) if first else None
    if name:
        p.name = name
        p.evidence.append(_ev("name", name, name, page_url, str(h1)[:300], 0.7))
    raw_price, frag = find_price(region)
    if raw_price:
        price = parse_price(raw_price)
        if price:
            p.price = price
            p.evidence.append(_ev("price", price.model_dump(mode="json"), raw_price, page_url, frag))
    text = region.get_text("\n", strip=True)
    for field, pat in LABELLED.items():
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip()
        if field == "gtin":
            p.gtin = normalize_gtin(raw)
            p.evidence.append(_ev("gtin", p.gtin, m.group(0), page_url, None, 0.75))
        elif field in ("weight", "dimensions"):
            meas = parse_measurement(raw)
            setattr(p, field, meas)
            p.evidence.append(_ev(field, meas.model_dump() if meas else None, m.group(0), page_url, None, 0.7))
        else:
            setattr(p, field, clean_text(raw, 200))
            p.evidence.append(_ev(field, getattr(p, field), m.group(0), page_url, None, 0.7))
    stock_el = region.find(True, attrs={"class": STOCK_CLASS})
    stock_text = clean_text(stock_el.get_text(" ", strip=True)) if stock_el else None
    av = normalize_availability(stock_text)
    if av:
        p.availability = av
        p.evidence.append(_ev("availability", av, stock_text, page_url, str(stock_el)[:300], 0.7))
    desc_el = region.find(True, attrs={"class": DESC_CLASS}) or region.find(True, attrs={"id": DESC_CLASS})
    desc = clean_text(desc_el.get_text(" ", strip=True), 5000) if desc_el else None
    if not desc:
        desc = clean_text(meta.get("description") or meta.get("og:description"), 5000)
    if desc:
        p.description = desc
        p.evidence.append(_ev("description", desc[:200], desc[:500], page_url, None, 0.6))
    seen = set()
    for img in region.find_all("img", src=True):
        src = img.get("data-src") or img["src"]
        if src.startswith("data:") or re.search(r"(logo|icon|sprite|spinner|placeholder|badge)", src, re.I):
            continue
        u = urljoin(page_url, src)
        if u not in seen:
            seen.add(u)
            p.images.append(ProductImage(url=u, alt=clean_text(img.get("alt"))))
        if len(p.images) >= 10:
            break
    if p.images:
        p.evidence.append(_ev("images", [i.url for i in p.images], ", ".join(i.url for i in p.images)[:500], page_url, None, 0.5))
    # definition lists as specifications
    for dl in region.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            attr = make_attribute(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True) if dd else None)
            if attr:
                p.attributes.append(attr)
                p.evidence.append(_ev(f"attributes.{attr.key}", attr.value, f"{attr.label}: {attr.raw_value}", page_url, str(dl)[:500], 0.7))
    # "Label: value" spec list items
    for li in region.select("ul li, ol li"):
        t = clean_text(li.get_text(" ", strip=True)) or ""
        m = re.match(r"^([A-Za-z][A-Za-z0-9 /()\-]{1,40})\s*:\s*(.{1,120})$", t)
        if m and m.group(1).strip().lower() not in ("sku", "brand", "price", "mpn", "ean", "upc", "gtin", "material", "weight", "manufacturer"):
            attr = make_attribute(m.group(1), m.group(2))
            if attr and not any(a.key == attr.key for a in p.attributes):
                p.attributes.append(attr)
                p.evidence.append(_ev(f"attributes.{attr.key}", attr.value, t, page_url, None, 0.6))
    return p if p.name else None


def product_cards(soup: BeautifulSoup, page_url: str) -> list[ExtractedProduct]:
    """Repeating product cards on listing pages (name + price inside one element)."""
    candidates: list[Tag] = []
    for el in soup.find_all(["div", "li", "article", "section"], attrs={"class": re.compile(r"(product|item|card|tile)", re.I)}):
        if PRICE_CLASS.search(" ".join(el.get("class", []))) and not el.find(["h2", "h3", "h4", "h5", "a"]):
            continue  # a price wrapper, not a card
        text = el.get_text(" ", strip=True)
        prices = find_price_strings(text)
        if len(prices) < 1 or len(text) > 800:
            continue
        # innermost element containing a single product
        # a nested element is a "more specific card" only if it has its own title/link, not just a price wrapper
        inner = [c for c in el.find_all(["div", "li", "article"], attrs={"class": re.compile(r"(product|item|card|tile)", re.I)})
                 if find_price_strings(c.get_text(" ", strip=True)) and c.find(["h2", "h3", "h4", "h5", "a"])]
        if inner:
            continue
        candidates.append(el)
    if len(candidates) < 2:
        return []
    out = []
    for el in candidates:
        heading = el.find(["h2", "h3", "h4", "h5"]) or el.find(attrs={"class": re.compile(r"(title|name)", re.I)}) or el.find("a")
        name = clean_text(heading.get_text(" ", strip=True), 300) if heading else None
        # visible card titles are often truncated ("A Light in the ..."); the link title holds the full name
        link = heading.find("a", title=True) if heading is not None and heading.name != "a" else heading
        full = clean_text(link.get("title"), 300) if link is not None and link.get("title") else None
        if full and name and (name.endswith(("...", "…")) or len(full) > len(name)) and full.startswith(name.rstrip(".… ")[:10]):
            name = full
        if not name or find_price_strings(name):
            continue
        frag = str(el)[:1200]
        p = ExtractedProduct(origin="html_card", source_url=page_url, name=name)
        p.evidence.append(_ev("name", name, name, page_url, frag, 0.6))
        raw_price, _ = find_price(el)
        price = parse_price(raw_price) if raw_price else None
        if price:
            p.price = price
            p.evidence.append(_ev("price", price.model_dump(mode="json"), raw_price, page_url, frag, 0.6))
        a = el.find("a", href=True)
        if a and not a["href"].startswith("#"):
            p.product_url = urljoin(page_url, a["href"])
            p.evidence.append(_ev("product_url", p.product_url, a["href"], page_url, frag, 0.6))
        img = el.find("img", src=True)
        if img:
            p.images.append(ProductImage(url=urljoin(page_url, img["src"]), alt=clean_text(img.get("alt"))))
            p.evidence.append(_ev("images", [p.images[0].url], img["src"], page_url, frag, 0.5))
        av = normalize_availability(el.get_text(" ", strip=True))
        if av:
            p.availability = av
            p.evidence.append(_ev("availability", av, av, page_url, frag, 0.5))
        out.append(p)
    return out
