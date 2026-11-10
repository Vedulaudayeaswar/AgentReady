"""Agent product search.

``SearchBackend`` is the abstraction; ``DatabaseSearchBackend`` is the implementation used today. It retrieves
candidates with SQL (LIKE on the main text fields and attribute values, plus pg_trgm word similarity on
PostgreSQL when the extension is installed), then applies filters that depend on the *public* representation
(effective prices at request time, public availability, visible attributes, variant options) and ranks in Python.

Ranking is transparent: every result carries its score and the individual signals; the weights are published
in ``RANKING_POLICY``. There is no paid placement. Merchant settings control eligibility only, never ranking.
A dedicated search engine can replace this backend later without changing the API.
"""

from __future__ import annotations

import difflib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import exists, false, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.products import contains_pattern
from app.models import Product, ProductAttribute
from app.services.commerce.catalog_rules import canonical_key
from app.services.commerce.money import aware, now_utc
from app.services.distribution.profile import MerchantScope
from app.services.distribution.public import AVAILABILITY_VALUES, eligibility_clause, public_product
from app.services.distribution.units import parse_number, parse_quantity
from app.services.normalization.normalize import attribute_key

log = logging.getLogger("agentready.search")

CANDIDATE_LIMIT = 2000
WEIGHTS = {"text": 0.45, "exact_identifier": 0.25, "brand": 0.10, "attributes": 0.05, "completeness": 0.10,
           "availability": 0.05}
RANKING_POLICY = {
    "weights": WEIGHTS,
    "signals": {
        "text": "Share of query words found in the name (strongest), brand, SKU/MPN, category, attributes or description",
        "exact_identifier": "1 when the query is exactly the product's SKU, GTIN or MPN",
        "brand": "1 when a query word is the product's brand",
        "attributes": "1 when attribute/variant filters were requested and matched",
        "completeness": "Share of key public fields that are filled in (description, brand, images, price, identifiers, category, availability, attributes)",
        "availability": "available 1.0, backorder/preorder 0.5, unknown 0.3, out of stock/discontinued 0",
    },
    "paid_placement": False,
    "note": "Merchant settings only decide whether a product is eligible; they never change its rank.",
}
STOPWORDS = {"a", "an", "the", "and", "or", "for", "with", "in", "of", "to", "on", "at", "by", "find", "show", "me",
             "under", "below", "over", "above", "less", "than", "more", "cheap", "best", "products", "product", "some",
             "any", "that", "are", "is", "be", "from", "which", "what", "i", "need", "want", "looking", "buy"}
FILTER_RE = re.compile(r"^\s*([A-Za-z0-9_ .-]{1,60}?)\s*(>=|<=|!=|>|<|:|=)\s*(.{1,200})\s*$")
_AVAIL_SCORE = {"available": 1.0, "backorder": 0.5, "preorder": 0.5, "unknown": 0.3, "out_of_stock": 0.0,
                "discontinued": 0.0}


class SearchError(ValueError):
    pass


@dataclass
class AttrFilter:
    key: str
    op: str
    value: str

    @staticmethod
    def parse(raw: str) -> "AttrFilter":
        m = FILTER_RE.match(raw or "")
        if not m:
            raise SearchError(f"Invalid filter '{raw}'. Use key:value, key=value, key>=value, key<=value, key>value, key<value or key!=value")
        return AttrFilter(attribute_key(m.group(1)), m.group(2), m.group(3).strip())

    def as_text(self) -> str:
        return f"{self.key}{self.op}{self.value}"


@dataclass
class SearchQuery:
    q: str | None = None
    brand: str | None = None
    sku: str | None = None
    gtin: str | None = None
    category: str | None = None  # path text (contains) or category id
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    currency: str | None = None
    availability: list[str] = field(default_factory=list)
    merchants: list[str] = field(default_factory=list)  # merchant slugs
    attributes: list[AttrFilter] = field(default_factory=list)
    variant_options: list[AttrFilter] = field(default_factory=list)
    updated_since: datetime | None = None
    product_ids: list[uuid.UUID] | None = None
    sort: str = "relevance"  # relevance|price_asc|price_desc|name|updated
    page: int = 1
    page_size: int = 20

    def validate(self) -> None:
        bad = [a for a in self.availability if a not in AVAILABILITY_VALUES]
        if bad:
            raise SearchError(f"Unknown availability value(s): {', '.join(bad)}. Use: {', '.join(AVAILABILITY_VALUES)}")
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise SearchError("min_price cannot be greater than max_price")
        if len(self.attributes) + len(self.variant_options) > 10:
            raise SearchError("At most 10 attribute/variant filters per request")
        if self.currency and not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise SearchError("currency must be an ISO 4217 code such as INR or USD")
        if self.q and len(self.q) > 200:
            raise SearchError("q is limited to 200 characters")

    def applied(self) -> dict:
        out = {k: v for k, v in {
            "q": self.q, "brand": self.brand, "sku": self.sku, "gtin": self.gtin, "category": self.category,
            "min_price": format(self.min_price, "f") if self.min_price is not None else None,
            "max_price": format(self.max_price, "f") if self.max_price is not None else None,
            "currency": self.currency, "availability": self.availability or None, "merchants": self.merchants or None,
            "attributes": [a.as_text() for a in self.attributes] or None,
            "variant_options": [a.as_text() for a in self.variant_options] or None,
            "updated_since": self.updated_since.isoformat() if self.updated_since else None,
        }.items() if v not in (None, [], "")}
        out["sort"] = self.sort
        return out


def tokenize(q: str | None) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", (q or "").lower())
    out = []
    for w in words:
        if w in STOPWORDS or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("es") and not w.endswith("ses"):
            w = w[:-2] if w[-3] in "sxz" or w.endswith(("ches", "shes")) else w[:-1]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return list(dict.fromkeys(out))[:12]


# ------------------------------------------------------------------ filter matching
def _values_match(f: AttrFilter, value: str | None, unit: str | None) -> bool:
    if value in (None, ""):
        return False
    fq = parse_quantity(f.value)
    vq = parse_quantity(value, unit)
    if fq is not None:
        if vq is None or vq.dimension != fq.dimension:
            return False  # incompatible units are never compared
        a, b = vq.value, fq.value
    elif (fn := parse_number(f.value)) is not None and (vn := parse_number(value)) is not None:
        a, b = vn, fn  # plain numbers: compared as written (no unit is assumed)
    else:
        sv, fv = _syn(str(value)), _syn(f.value)
        if f.op == ":":
            return sv == fv or re.search(rf"(?<![a-z0-9]){re.escape(fv)}(?![a-z0-9])", sv) is not None
        if f.op == "=":
            return sv == fv
        if f.op == "!=":
            return sv != fv
        return False  # ordering operators need numbers
    return {":": a == b, "=": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[f.op]


SIZE_SYNONYMS = {"extra small": "xs", "x-small": "xs", "small": "s", "medium": "m", "med": "m", "large": "l",
                 "extra large": "xl", "x-large": "xl", "xx-large": "xxl", "double extra large": "xxl"}


def _syn(v: str) -> str:
    v = v.strip().lower()
    return SIZE_SYNONYMS.get(v, v)


def _key_matches(fkey: str, key: str | None, label: str | None) -> bool:
    ck = canonical_key(fkey)
    return canonical_key(key or "") == ck or (label is not None and canonical_key(attribute_key(label)) == ck)


def _product_level_values(pub: dict, key: str) -> list[tuple[str | None, str | None]]:
    vals = [(a["value"], a["unit"]) for a in pub["attributes"] if _key_matches(key, a["key"], a["name"])]
    ck = canonical_key(key)
    if ck in ("brand", "manufacturer") and pub.get(ck):
        vals.append((pub[ck], None))
    if ck == "material" and (pub.get("specifications") or {}).get("material"):
        vals.append((pub["specifications"]["material"], None))
    return vals


def attribute_filters_match(pub: dict, filters: list[AttrFilter]) -> bool:
    for f in filters:
        vals = _product_level_values(pub, f.key)
        if f.op == "!=":
            # unknown values never satisfy a filter
            if not vals or not all(_values_match(f, v, u) for v, u in vals):
                return False
        elif not any(_values_match(f, v, u) for v, u in vals):
            return False
    return True


def variant_filters_match(pub: dict, filters: list[AttrFilter]) -> tuple[bool, list[str]]:
    """All filters must hold for the same variant (or for the product itself when it has no such option)."""
    variants = pub["variants"]
    candidates = list(variants)
    used_variants = False
    for f in filters:
        if any(any(_key_matches(f.key, k, k) for k in (v["options"] or {})) for v in variants):
            used_variants = True
            candidates = [v for v in candidates if any(_key_matches(f.key, k, k) and _values_match(f, str(val), None)
                                                       for k, val in (v["options"] or {}).items())]
            if not candidates:
                return False, []
        elif not any(_values_match(f, v, u) for v, u in _product_level_values(pub, f.key)):
            return False, []
    return True, [v["id"] for v in candidates] if used_variants else []


def filter_price(pub: dict) -> tuple[Decimal | None, str | None]:
    if pub.get("price"):
        return Decimal(pub["price"]["current"]), pub["price"]["currency"]
    prices = [(Decimal(v["price"]["current"]), v["price"]["currency"]) for v in pub.get("variants", []) if v.get("price")]
    return min(prices) if prices else (None, None)


# ------------------------------------------------------------------ ranking
def completeness(pub: dict) -> float:
    checks = [bool(pub["description"]), bool(pub["brand"]), bool(pub["images"]), pub.get("price") is not None,
              bool(pub["identifiers"]["sku"] or pub["identifiers"]["gtin"]), pub["category"] is not None,
              bool(pub.get("availability")) and pub["availability"]["status"] != "unknown", bool(pub["attributes"])]
    return round(sum(checks) / len(checks), 3)


def _fuzzy_in(token: str, words: list[str]) -> bool:
    return len(token) >= 4 and any(difflib.SequenceMatcher(None, token, w).ratio() >= 0.8 for w in words if len(w) >= 3)


def rank(pub: dict, tokens: list[str], raw_q: str | None, has_filters: bool) -> dict:
    name = (pub["name"] or "").lower()
    fields = [
        (1.0, name), (0.8, (pub["brand"] or "").lower()),
        (0.8, " ".join(filter(None, [pub["identifiers"]["sku"], pub["identifiers"]["mpn"], pub["identifiers"]["gtin"]])).lower()),
        (0.6, ((pub["category"] or {}).get("path") or "").lower()),
        (0.6, " ".join(f"{a['name']} {a['value']} {a['unit'] or ''}" for a in pub["attributes"]).lower()),
        (0.6, " ".join(" ".join(map(str, (v["options"] or {}).values())) for v in pub["variants"]).lower()),
        (0.3, (pub["description"] or "").lower()),
    ]
    name_words = re.findall(r"[a-z0-9]+", name)
    text_score = 0.0
    if tokens:
        per = []
        for t in tokens:
            best = max((w for w, txt in fields if t in txt), default=0.0)
            if best == 0.0 and _fuzzy_in(t, name_words):
                best = 0.5
            per.append(best)
        text_score = sum(per) / len(per)
    q = (raw_q or "").strip().lower()
    ident = 1.0 if q and q in {(pub["identifiers"][k] or "").lower() for k in ("sku", "gtin", "mpn")} else 0.0
    brand = 1.0 if pub["brand"] and (pub["brand"].lower() in tokens or pub["brand"].lower() == q) else 0.0
    avail = _AVAIL_SCORE.get((pub.get("availability") or {}).get("status", "unknown"), 0.3)
    signals = {"text": round(text_score, 3), "exact_identifier": ident, "brand": brand,
               "attributes": 1.0 if has_filters else 0.0, "completeness": completeness(pub), "availability": avail}
    score = sum(WEIGHTS[k] * v for k, v in signals.items())
    return {"score": round(score, 4), "signals": signals}


# ------------------------------------------------------------------ backend
class SearchBackend(Protocol):
    name: str

    async def search(self, db: AsyncSession, scopes: list[MerchantScope], channel: str, query: SearchQuery) -> dict: ...


_TRGM: dict[str, bool] = {}


async def _trgm_available(db: AsyncSession) -> bool:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    key = str(bind.url)
    if key not in _TRGM:
        try:
            _TRGM[key] = bool((await db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))).first())
        except Exception:  # pragma: no cover
            _TRGM[key] = False
    return _TRGM[key]


class DatabaseSearchBackend:
    name = "database"

    def _token_clause(self, token: str, trgm: bool):
        like = contains_pattern(token)
        cols = [Product.name, Product.brand, Product.sku, Product.gtin, Product.mpn, Product.category,
                Product.manufacturer, Product.product_type, Product.description]
        conds = [func.lower(c).like(like, escape="\\") for c in cols]
        conds.append(exists().where(ProductAttribute.product_id == Product.id, or_(
            func.lower(ProductAttribute.value).like(like, escape="\\"),
            func.lower(ProductAttribute.label).like(like, escape="\\"))))
        if trgm and len(token) >= 4:
            conds.append(func.word_similarity(token, func.lower(Product.name)) >= 0.6)
        return or_(*conds)

    async def _candidates(self, db, scope: MerchantScope, channel: str, query: SearchQuery, tokens: list[str],
                          mode: str, trgm: bool) -> list[Product]:
        stmt = select(Product).where(eligibility_clause(scope, channel))
        if query.product_ids is not None:
            stmt = stmt.where(Product.id.in_(query.product_ids))
        if query.brand:
            stmt = stmt.where(func.lower(Product.brand) == query.brand.lower())
        if query.sku:
            stmt = stmt.where(func.lower(Product.sku) == query.sku.lower())
        if query.gtin:
            stmt = stmt.where(Product.gtin == re.sub(r"\D", "", query.gtin))
        if query.category:
            try:
                cid = uuid.UUID(query.category)
            except ValueError:
                cid = None
            if cid is not None:
                root = scope.category_paths.get(cid)
                ids = {cid} | ({c for c, path in scope.category_paths.items()
                                if path.lower().startswith(root.lower() + " / ")} if root else set())
                stmt = stmt.where(Product.category_id.in_(list(ids)))
            else:
                like = contains_pattern(query.category)
                ids = [c for c, path in scope.category_paths.items() if query.category.lower() in path.lower()]
                stmt = stmt.where(or_(func.lower(Product.category).like(like, escape="\\"),
                                      Product.category_id.in_(ids) if ids else false()))
        if query.updated_since:
            stmt = stmt.where(Product.updated_at > query.updated_since)
        if tokens:
            clauses = [self._token_clause(t, trgm) for t in tokens]
            stmt = stmt.where(*clauses) if mode == "all" else stmt.where(or_(*clauses))
        return list((await db.execute(stmt.order_by(Product.updated_at.desc(), Product.id).limit(CANDIDATE_LIMIT + 1))).scalars().all())

    async def search(self, db: AsyncSession, scopes: list[MerchantScope], channel: str, query: SearchQuery) -> dict:
        query.validate()
        at = now_utc()
        tokens = tokenize(query.q)
        if query.merchants:
            wanted = {m.lower() for m in query.merchants}
            scopes = [s for s in scopes if s.profile.slug.lower() in wanted]
        trgm = await _trgm_available(db)
        mode = "all"
        rows: list[tuple[Product, MerchantScope]] = []
        truncated = False
        for attempt in ("all", "any") if len(tokens) > 1 else ("all",):
            mode, rows, truncated = attempt, [], False
            for sc in scopes:
                cands = await self._candidates(db, sc, channel, query, tokens, attempt, trgm)
                truncated |= len(cands) > CANDIDATE_LIMIT
                rows.extend((p, sc) for p in cands[:CANDIDATE_LIMIT])
            if rows:
                break
        has_filters = bool(query.attributes or query.variant_options)
        results = []
        currencies: set[str] = set()
        for p, sc in rows:
            pub = public_product(p, sc, at)
            if query.availability:
                st = (pub.get("availability") or {}).get("status")
                if st is None or st not in query.availability:
                    continue
            if query.attributes and not attribute_filters_match(pub, query.attributes):
                continue
            matched_variants: list[str] = []
            if query.variant_options:
                ok, matched_variants = variant_filters_match(pub, query.variant_options)
                if not ok:
                    continue
            price, cur = filter_price(pub)
            if query.currency and (cur is None or cur != query.currency):
                continue
            if cur:
                currencies.add(cur)  # before the price filter: amounts in other currencies must not be compared
            if query.min_price is not None or query.max_price is not None:
                if price is None:
                    continue
                if query.min_price is not None and price < query.min_price:
                    continue
                if query.max_price is not None and price > query.max_price:
                    continue
            results.append({"product": pub, "ranking": rank(pub, tokens, query.q, has_filters),
                            "matched_variant_ids": matched_variants, "_price": price})
        if (query.min_price is not None or query.max_price is not None or query.sort.startswith("price")) \
                and not query.currency and len(currencies) > 1:
            raise SearchError(f"Results use several currencies ({', '.join(sorted(currencies))}). Prices are never "
                              "converted between currencies: pass currency= to filter or sort by price.")
        if query.sort == "relevance":
            results.sort(key=lambda r: (-r["ranking"]["score"], (r["product"]["name"] or "").lower(), r["product"]["id"]))
        elif query.sort in ("price_asc", "price_desc"):
            priced = [r for r in results if r["_price"] is not None]
            unpriced = [r for r in results if r["_price"] is None]
            priced.sort(key=lambda r: (r["_price"], r["product"]["id"]), reverse=query.sort == "price_desc")
            results = priced + unpriced
        elif query.sort == "name":
            results.sort(key=lambda r: ((r["product"]["name"] or "").lower(), r["product"]["id"]))
        elif query.sort == "updated":
            results.sort(key=lambda r: (r["product"]["updated_at"] or "", r["product"]["id"]), reverse=True)
        else:
            raise SearchError("sort must be relevance, price_asc, price_desc, name or updated")
        total = len(results)
        start = (query.page - 1) * query.page_size
        page = results[start:start + query.page_size]
        for r in page:
            r.pop("_price", None)
        return {"items": page, "total": total, "page": query.page, "page_size": query.page_size,
                "has_more": start + query.page_size < total, "query": query.applied(), "match_mode": mode,
                "tokens": tokens, "candidates_truncated": truncated, "search_backend": self.name,
                "fuzzy_matching": "pg_trgm" if trgm else "difflib (ranking only)", "ranking_policy": RANKING_POLICY,
                "as_of": at.isoformat(), "valid_until": _valid_until([r["product"] for r in page], at)}


def _valid_until(pubs: list[dict], at: datetime) -> str | None:
    ts = [p["price"]["valid_until"] for p in pubs if p.get("price") and p["price"].get("valid_until")]
    return min(ts) if ts else None


_BACKEND: SearchBackend = DatabaseSearchBackend()


def get_backend() -> SearchBackend:
    return _BACKEND


__all__ = ["AttrFilter", "SearchQuery", "SearchError", "get_backend", "RANKING_POLICY", "tokenize", "aware"]
