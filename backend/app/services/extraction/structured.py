"""Deterministic extraction from structured markup: JSON-LD, microdata and meta tags."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.schemas.canonical import (
    ExtractedProduct,
    ProductEvidence,
    ProductImage,
    ProductVariant,
)
from app.services.normalization.normalize import (
    clean_text,
    make_attribute,
    normalize_availability,
    normalize_currency_code,
    normalize_gtin,
    parse_measurement,
    parse_price,
)

PRODUCT_TYPES = {"product", "productgroup", "productmodel", "individualproduct", "vehicle", "car"}


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    vals = t if isinstance(t, list) else [t]
    return {str(v).rsplit("/", 1)[-1].lower() for v in vals if v}


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return _text(v.get("name") or v.get("@value") or v.get("value"))
    if isinstance(v, list):
        return _text(v[0]) if v else None
    return clean_text(str(v))


def _walk_jsonld(data: Any, out: list[dict]) -> None:
    if isinstance(data, list):
        for d in data:
            _walk_jsonld(d, out)
    elif isinstance(data, dict):
        if _types(data) & PRODUCT_TYPES:
            out.append(data)
            return  # variants handled by the product mapper
        for key in ("@graph", "itemListElement", "item", "mainEntity", "about", "offers"):
            if key in data:
                _walk_jsonld(data[key], out)


def parse_jsonld_blocks(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = script.string or script.get_text() or ""
        raw = raw.strip().rstrip(";")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # tolerate a common malformation: trailing commas
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        _walk_jsonld(data, nodes)
    return nodes


def _ev(field: str, value: Any, source_value: Any, url: str, fragment: str | None, stype: str = "json_ld"):
    sv = source_value if isinstance(source_value, str) else json.dumps(source_value, ensure_ascii=False, default=str)
    return ProductEvidence(
        field=field,
        value=value,
        source_value=(sv or "")[:500],
        source_url=url,
        source_fragment=(fragment or "")[:1500] or None,
        source_type=stype,  # type: ignore[arg-type]
        extraction_method="deterministic",
        confidence=0.95 if stype == "json_ld" else 0.85,
        verified_in_source=True,
    )


def _images(v: Any, base: str) -> list[ProductImage]:
    items = v if isinstance(v, list) else [v]
    out = []
    for it in items:
        if isinstance(it, str):
            out.append(ProductImage(url=urljoin(base, it)))
        elif isinstance(it, dict):
            u = it.get("url") or it.get("contentUrl")
            if u:
                out.append(ProductImage(url=urljoin(base, str(u)), alt=_text(it.get("caption") or it.get("name"))))
    return out[:20]


def _offers(v: Any) -> list[dict]:
    if isinstance(v, dict):
        if "offers" in v and _types(v) & {"aggregateoffer"}:
            inner = _offers(v["offers"])
            return inner or [v]
        return [v]
    if isinstance(v, list):
        return [o for o in v if isinstance(o, dict)]
    return []


def map_jsonld_product(node: dict, page_url: str) -> ExtractedProduct:
    frag = json.dumps(node, ensure_ascii=False, default=str)[:1500]
    p = ExtractedProduct(origin="json_ld", source_url=page_url)
    evid = p.evidence

    def setf(field: str, value: Any, raw: Any):
        if value in (None, "", []):
            return
        setattr(p, field, value)
        evid.append(_ev(field, value if not hasattr(value, "model_dump") else value.model_dump(mode="json"), raw, page_url, frag))

    setf("name", clean_text(_text(node.get("name")), 500), node.get("name"))
    setf("sku", clean_text(_text(node.get("sku")), 200), node.get("sku"))
    setf("brand", clean_text(_text(node.get("brand")), 200), node.get("brand"))
    setf("manufacturer", clean_text(_text(node.get("manufacturer")), 200), node.get("manufacturer"))
    setf("category", clean_text(_text(node.get("category")), 300), node.get("category"))
    setf("description", clean_text(_text(node.get("description")), 5000), node.get("description"))
    setf("mpn", clean_text(_text(node.get("mpn")), 100), node.get("mpn"))
    setf("material", clean_text(_text(node.get("material")), 200), node.get("material"))
    for gk in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin", "ean", "upc"):
        if node.get(gk):
            setf("gtin", normalize_gtin(_text(node.get(gk))), node.get(gk))
            break
    first_offer = node.get("offers") if isinstance(node.get("offers"), dict) else {}
    url = node.get("url") or first_offer.get("url")
    if url:
        setf("product_url", urljoin(page_url, str(url)), url)
    imgs = _images(node.get("image"), page_url)
    if imgs:
        p.images = imgs
        evid.append(_ev("images", [i.url for i in imgs], node.get("image"), page_url, frag))
    for mk in ("weight", "depth", "width", "height"):
        mv = node.get(mk)
        if isinstance(mv, dict):
            raw = f"{mv.get('value', '')} {mv.get('unitText') or mv.get('unitCode') or ''}".strip()
        else:
            raw = _text(mv)
        if not raw:
            continue
        if mk == "weight":
            setf("weight", parse_measurement(raw), mv)
        else:
            attr = make_attribute(mk.capitalize(), raw)
            if attr:
                p.attributes.append(attr)
                evid.append(_ev(f"attributes.{attr.key}", attr.value, mv, page_url, frag))

    offers = _offers(node.get("offers"))
    if offers:
        o = offers[0]
        price_raw = o.get("price") if o.get("price") not in (None, "") else o.get("lowPrice")
        if price_raw is None and isinstance(o.get("priceSpecification"), dict):
            price_raw = o["priceSpecification"].get("price")
        spec = o.get("priceSpecification") if isinstance(o.get("priceSpecification"), dict) else {}
        cur = o.get("priceCurrency") or spec.get("priceCurrency")
        price = parse_price(price_raw, normalize_currency_code(_text(cur)))
        if price:
            setf("price", price, {"price": price_raw, "priceCurrency": cur})
        avail = normalize_availability(_text(o.get("availability")))
        setf("availability", avail, o.get("availability"))
        distinct = {str(x.get("price")) for x in offers if x.get("price") is not None}
        if len(distinct) > 1:
            p.flags.append({"code": "multiple_offers", "message": f"{len(offers)} offers with different prices", "values": sorted(distinct)})

    for prop in node.get("additionalProperty") or []:
        if isinstance(prop, dict) and prop.get("name"):
            raw = f"{_text(prop.get('value')) or ''} {prop.get('unitText') or ''}".strip()
            attr = make_attribute(str(prop["name"]), raw)
            if attr:
                p.attributes.append(attr)
                evid.append(_ev(f"attributes.{attr.key}", attr.value, prop, page_url, frag))

    for v in node.get("hasVariant") or []:
        if not isinstance(v, dict):
            continue
        vo = _offers(v.get("offers"))
        vp = parse_price(vo[0].get("price"), _text(vo[0].get("priceCurrency"))) if vo else None
        attrs = {}
        for k in ("color", "size", "pattern", "material"):
            if v.get(k):
                attrs[k] = _text(v.get(k)) or ""
        p.variants.append(
            ProductVariant(
                name=_text(v.get("name")),
                sku=_text(v.get("sku")),
                price=vp,
                availability=normalize_availability(_text(vo[0].get("availability"))) if vo else None,
                attributes=attrs,
            )
        )
    if p.variants and p.price is None:
        priced = [v.price for v in p.variants if v.price and v.price.amount is not None]
        if priced:
            low = min(priced, key=lambda x: x.amount)
            p.price = low
            evid.append(_ev("price", low.model_dump(mode="json"), "lowest variant price", page_url, frag))
            p.flags.append({"code": "price_from_variants", "message": "Price taken from the lowest-priced variant"})
    return p


# ---------------------------------------------------------------- microdata
def _itemprop(scope: Tag, name: str) -> Tag | None:
    for el in scope.find_all(attrs={"itemprop": True}):
        props = el.get("itemprop", "").split()
        if name in props:
            # ensure the property belongs to this scope, not a nested item
            parent_scope = el.find_parent(attrs={"itemscope": True})
            if parent_scope is scope or (name in ("price", "priceCurrency", "availability") and parent_scope is not None):
                return el
    return None


def _prop_value(el: Tag | None) -> str | None:
    if el is None:
        return None
    for attr in ("content", "href", "src", "value", "datetime"):
        if el.get(attr):
            return clean_text(el.get(attr))
    return clean_text(el.get_text(" ", strip=True))


def parse_microdata(soup: BeautifulSoup, page_url: str) -> list[ExtractedProduct]:
    out = []
    for scope in soup.find_all(attrs={"itemscope": True, "itemtype": re.compile(r"schema\.org/(Product|ProductModel|IndividualProduct)", re.I)}):
        frag = str(scope)[:1500]
        p = ExtractedProduct(origin="microdata", source_url=page_url)
        for field in ("name", "sku", "brand", "description", "mpn", "category", "material"):
            val = _prop_value(_itemprop(scope, field))
            if val:
                setattr(p, field, val[:5000] if field == "description" else val[:500])
                p.evidence.append(_ev(field, val, val, page_url, frag, "microdata"))
        for gk in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin"):
            val = _prop_value(_itemprop(scope, gk))
            if val:
                p.gtin = normalize_gtin(val)
                p.evidence.append(_ev("gtin", p.gtin, val, page_url, frag, "microdata"))
                break
        price_raw = _prop_value(_itemprop(scope, "price"))
        cur = _prop_value(_itemprop(scope, "priceCurrency"))
        if price_raw:
            price = parse_price(price_raw, cur)
            if price:
                p.price = price
                p.evidence.append(_ev("price", price.model_dump(mode="json"), f"{price_raw} {cur or ''}".strip(), page_url, frag, "microdata"))
        av = normalize_availability(_prop_value(_itemprop(scope, "availability")))
        if av:
            p.availability = av
            p.evidence.append(_ev("availability", av, av, page_url, frag, "microdata"))
        img = _prop_value(_itemprop(scope, "image"))
        if img:
            p.images = [ProductImage(url=urljoin(page_url, img))]
            p.evidence.append(_ev("images", [p.images[0].url], img, page_url, frag, "microdata"))
        if p.name:
            out.append(p)
    return out


# ---------------------------------------------------------------- meta tags
def parse_meta(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        val = m.get("content")
        if key and val and key not in meta:
            meta[key] = val.strip()
    return meta


def meta_product(meta: dict[str, str], page_url: str) -> ExtractedProduct | None:
    is_product = meta.get("og:type", "").lower().startswith("product") or "product:price:amount" in meta
    if not is_product:
        return None
    p = ExtractedProduct(origin="meta", source_url=page_url)

    def add(field, value, raw):
        if value:
            setattr(p, field, value)
            p.evidence.append(_ev(field, value if not hasattr(value, "model_dump") else value.model_dump(mode="json"), raw, page_url, None, "meta"))

    add("name", clean_text(meta.get("og:title"), 500), meta.get("og:title"))
    add("description", clean_text(meta.get("og:description"), 5000), meta.get("og:description"))
    amount = meta.get("product:price:amount") or meta.get("og:price:amount")
    cur = meta.get("product:price:currency") or meta.get("og:price:currency")
    if amount:
        add("price", parse_price(amount, cur), f"{amount} {cur or ''}".strip())
    add("availability", normalize_availability(meta.get("product:availability") or meta.get("og:availability")), meta.get("product:availability"))
    add("brand", clean_text(meta.get("product:brand") or meta.get("og:brand")), meta.get("product:brand"))
    if meta.get("og:image"):
        p.images = [ProductImage(url=urljoin(page_url, meta["og:image"]))]
        p.evidence.append(_ev("images", [p.images[0].url], meta["og:image"], page_url, None, "meta"))
    return p
