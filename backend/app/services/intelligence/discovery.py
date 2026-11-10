"""Product discovery intelligence, computed from the Phase 3 ``distribution_events`` (no second pipeline).

Every number is a count of requests AgentReady actually served. These are *observed interest signals* from external
agents; they are not shopper demand, impressions, purchases or conversions.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DistributionEvent, Product, live_products
from app.services.commerce.catalog_rules import canonical_key
from app.services.commerce.money import aware, now_utc
from app.services.distribution.search import AttrFilter, SearchError, tokenize

INTEREST_KINDS = ("product_detail", "compare", "availability", "pricing", "variants")
SEARCH_KINDS = ("search", "recommend")
FEW_RESULTS = 2
DISCLAIMER = ("Counts are requests that external agents sent to AgentReady. They show interest from agents, not shopper "
              "demand, impressions, purchases or conversions.")


async def _events(db, business_id, days, kinds):
    since = now_utc() - timedelta(days=days)
    return list((await db.execute(select(DistributionEvent).where(
        DistributionEvent.business_id == business_id, DistributionEvent.created_at >= since,
        DistributionEvent.kind.in_(kinds) | ((DistributionEvent.kind == "mcp_call")))
        .order_by(DistributionEvent.created_at))).scalars())


def _tool_kind(e: DistributionEvent) -> str:
    if e.kind != "mcp_call":
        return e.kind
    tool = (e.meta or {}).get("tool", "")
    if tool in SEARCH_KINDS + INTEREST_KINDS + ("catalog", "merchants", "categories"):
        return tool  # MCP events record the underlying operation name
    return {"search_products": "search", "recommend_products": "recommend", "get_product_details": "product_detail",
            "compare": "compare", "compare_products": "compare", "check_product_availability": "availability",
            "get_product_pricing": "pricing", "get_product_variants": "variants", "get_merchant_catalog": "catalog",
            "search_merchants": "merchants"}.get(tool, "mcp_other")


async def search_insights(db: AsyncSession, business_id: uuid.UUID, days: int = 30) -> dict:
    events = await _events(db, business_id, days, SEARCH_KINDS)
    searches = [e for e in events if _tool_kind(e) in SEARCH_KINDS and e.status_code in (None, 200)]
    terms, queries, filters, attrs, variants, cats = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    zero: dict[str, dict] = {}
    few: dict[str, dict] = {}
    for e in searches:
        m = e.meta or {}
        q = (m.get("q") or "").strip().lower()
        for t in tokenize(q):
            terms[t] += 1
        key = q or "(no keywords)"
        afs = m.get("attribute_filters") or []
        label = " ".join([key] + [f"[{a}]" for a in afs])
        queries[label] += 1
        for f in m.get("filters") or []:
            filters[f] += 1
        for a in m.get("attributes") or []:
            attrs[a] += 1
        for v in m.get("variant_options") or []:
            variants[v] += 1
        if e.category:
            cats[e.category] += 1
        n = e.result_count if e.result_count is not None else m.get("total")
        bucket = zero if n == 0 else few if n is not None and n <= FEW_RESULTS else None
        if bucket is not None:
            b = bucket.setdefault(label, {"query": q, "attribute_filters": afs, "count": 0, "last_seen": None, "results": n})
            b["count"] += 1
            b["last_seen"] = aware(e.created_at).isoformat()
    return {
        "period_days": days, "searches": len(searches), "disclaimer": DISCLAIMER,
        "top_terms": [{"term": t, "count": c} for t, c in terms.most_common(20)],
        "top_queries": [{"query": q, "count": c} for q, c in queries.most_common(20)],
        "zero_result_queries": sorted(zero.values(), key=lambda x: -x["count"])[:50],
        "few_result_queries": sorted(few.values(), key=lambda x: -x["count"])[:50],
        "filters_used": [{"filter": f, "count": c} for f, c in filters.most_common()],
        "attributes_requested": [{"attribute": a, "count": c} for a, c in attrs.most_common(20)],
        "variant_options_requested": [{"option": a, "count": c} for a, c in variants.most_common(20)],
        "categories_requested": [{"category": c, "count": n} for c, n in cats.most_common(20)],
    }


async def product_interest(db: AsyncSession, business_id: uuid.UUID, days: int = 30, limit: int = 50) -> dict:
    events = [e for e in await _events(db, business_id, days, INTEREST_KINDS) if e.product_id is not None]
    per: dict[uuid.UUID, Counter] = defaultdict(Counter)
    for e in events:
        k = _tool_kind(e)
        if k in INTEREST_KINDS:
            per[e.product_id][k] += 1
    names = dict((await db.execute(select(Product.id, Product.name).where(Product.id.in_(list(per)))) ).all()) if per else {}
    rows = [{"product_id": str(pid), "name": names.get(pid), **{k: c.get(k, 0) for k in INTEREST_KINDS},
             "total": sum(c.values())} for pid, c in per.items()]
    rows.sort(key=lambda r: -r["total"])
    by_cat = Counter(e.category for e in events if e.category)
    return {"period_days": days, "disclaimer": DISCLAIMER, "products": rows[:limit],
            "categories": [{"category": c, "requests": n} for c, n in by_cat.most_common(20)]}


async def activity_trend(db: AsyncSession, business_id: uuid.UUID, days: int = 30) -> list[dict]:
    since = now_utc() - timedelta(days=days)
    rows = (await db.execute(select(DistributionEvent.created_at, DistributionEvent.kind).where(
        DistributionEvent.business_id == business_id, DistributionEvent.created_at >= since))).all()
    out: dict[str, Counter] = defaultdict(Counter)
    for at, kind in rows:
        group = "searches" if kind in SEARCH_KINDS else "product_requests" if kind in INTEREST_KINDS else \
            "mcp_calls" if kind == "mcp_call" else "feed_downloads" if kind == "feed_download" else None
        if group:
            out[aware(at).date().isoformat()][group] += 1
    return [{"day": d, **c} for d, c in sorted(out.items())]


_UNIT_TOKEN = re.compile(r"^\d+(?:\.\d+)?[a-z]+$")


async def zero_result_opportunities(db: AsyncSession, business_id: uuid.UUID, days: int = 30) -> list[dict]:
    """For each zero-result query, look at the merchant's OWN catalog (including products not distributed) to explain
    why nothing was returned. Never assumes the merchant sells what was asked for."""
    insights = await search_insights(db, business_id, days)
    products = list((await db.execute(select(Product).where(*live_products(business_id)))).scalars())
    out = []
    for z in insights["zero_result_queries"][:25]:
        tokens = [t for t in tokenize(z["query"]) if not _UNIT_TOKEN.match(t)]
        matches = [p for p in products if tokens and all(t in (f"{p.name or ''} {p.category or ''} {p.brand or ''} "
                                                                 f"{p.product_type or ''}").lower() for t in tokens)]
        requested = []
        for raw in z.get("attribute_filters") or []:
            try:
                requested.append(AttrFilter.parse(raw).key)
            except SearchError:
                continue
        missing_attr = [p for p in matches if requested and not all(
            any(canonical_key(a.key) == canonical_key(k) and a.value for a in p.attributes) for k in requested)]
        not_distributed = [p for p in matches if not (p.approval_status == "approved" and p.publication_status == "published")]
        if not matches:
            finding = "No product in your catalog matches these keywords. This may simply be something you do not sell."
            kind = "no_matching_products"
        elif missing_attr:
            finding = (f"{len(missing_attr)} product(s) match the keywords but lack the requested "
                       f"{', '.join(dict.fromkeys(requested))} attribute, so attribute-filtered searches cannot return them.")
            kind = "missing_attribute"
        elif not_distributed:
            finding = f"{len(not_distributed)} matching product(s) exist but are not approved and published."
            kind = "not_distributed"
        else:
            finding = ("Matching products exist; the requested values (price, availability or attribute values) did not match, "
                       "or distribution settings exclude them.")
            kind = "values_did_not_match"
        affected = missing_attr or not_distributed or matches
        out.append({**z, "finding": finding, "kind": kind, "matching_products": len(matches),
                    "affected": [{"id": str(p.id), "name": p.name} for p in affected[:20]],
                    "requested_attributes": list(dict.fromkeys(requested))})
    return out
