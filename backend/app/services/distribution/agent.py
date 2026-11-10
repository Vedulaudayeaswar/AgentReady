"""Agent-facing operations shared by the REST agent API (/api/v3/agents) and the MCP server.

Who can see what (enforced here, server-side):

* **API key** with the ``agent:read`` scope — that merchant's distributed products (the merchant must have
  enabled distribution and the channel).
* **Merchant session** — the merchant's own distributed products (used by the dashboard's "try it" views).
* **Anonymous** — only merchants that opted into the public network; IP rate limited.

Every response is built with the canonical public serializer, so private data (cost prices, stock quantities,
evidence, notes, credentials) can never appear.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Category, DistributionProfile, Product, ScheduledPriceChange
from app.services.commerce.money import now_utc
from app.services.distribution import analytics, cache
from app.services.distribution.compare import compare as compare_products
from app.services.distribution.profile import MerchantScope, channel_open, merchant_scope
from app.services.distribution.public import SCHEMA, eligibility_clause, public_product
from app.services.distribution.search import SearchError, SearchQuery, get_backend

API_VERSION = "3.0"


class AgentError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@dataclass
class AgentContext:
    mode: str  # api_key|merchant|public
    channel: str = "agent_api"  # agent_api|mcp
    business: Business | None = None
    api_key_id: str | None = None
    label: str | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def consumer(self) -> analytics.Consumer:
        if self.mode == "api_key":
            return analytics.Consumer("api_key", self.api_key_id, self.label)
        if self.mode == "merchant":
            return analytics.Consumer("merchant", None, "Merchant dashboard")
        return analytics.Consumer("anonymous", None, self.label or "Anonymous public agents")


async def scopes_for(db: AsyncSession, actx: AgentContext) -> list[MerchantScope]:
    if actx.mode in ("api_key", "merchant"):
        prof = (await db.execute(select(DistributionProfile).where(
            DistributionProfile.business_id == actx.business.id))).scalar_one_or_none()
        ok, reason = channel_open(prof, actx.channel)
        if not ok:
            raise AgentError(403, "distribution_disabled", reason or "Distribution is disabled")
        return [await merchant_scope(db, actx.business, prof)]
    profs = (await db.execute(select(DistributionProfile).where(DistributionProfile.enabled.is_(True),
                                                                 DistributionProfile.public_network.is_(True)))).scalars().all()
    out = []
    for prof in profs:
        if channel_open(prof, actx.channel)[0]:
            b = await db.get(Business, prof.business_id)
            if b is not None:
                out.append(await merchant_scope(db, b, prof))
    return out


async def ensure_fresh_prices(db: AsyncSession, scopes: list[MerchantScope]) -> None:
    """Apply scheduled price changes that are due but not yet applied by the scheduler tick."""
    ids = [s.business_id for s in scopes]
    if not ids:
        return
    due = (await db.execute(select(func.count()).select_from(ScheduledPriceChange).where(
        ScheduledPriceChange.business_id.in_(ids), ScheduledPriceChange.status == "scheduled",
        ScheduledPriceChange.effective_at <= now_utc()))).scalar_one()
    if due:
        from app.services.commerce.pricing import apply_due_price_changes

        await apply_due_price_changes(db, business_ids=ids, limit=100)


def _envelope(data: dict, scopes: list[MerchantScope]) -> dict:
    return {"schema": SCHEMA, "api_version": API_VERSION, "generated_at": now_utc().isoformat(), **data}


async def _record(db, actx: AgentContext, scopes, kind: str, started: float, *, product_id=None, category=None,
                  result_count=None, per_merchant: dict | None = None, meta=None, status=200):
    ms = int((time.monotonic() - started) * 1000)
    kind_ = "mcp_call" if actx.channel == "mcp" else kind
    meta = {**(meta or {}), **({"tool": kind} if actx.channel == "mcp" else {})}
    targets = per_merchant if per_merchant is not None else {s.business_id: result_count for s in scopes}
    for bid, n in targets.items():
        analytics.record(db, bid, kind_, actx.channel, actx.consumer, product_id=product_id, category=category,
                         status_code=status, latency_ms=ms, result_count=n, meta=meta)
    await db.commit()


async def record_error(db, actx: AgentContext, kind: str, status: int, message: str) -> None:
    """Errors are attributed to the keyed merchant (anonymous errors have no merchant to attribute to)."""
    if actx.business is not None:
        analytics.record(db, actx.business.id, "error", actx.channel, actx.consumer, status_code=status,
                         meta={"error": message[:200], "operation": kind})
        await db.commit()


# ------------------------------------------------------------------ operations
async def search(db: AsyncSession, actx: AgentContext, query: SearchQuery, kind: str = "search") -> dict:
    started = time.monotonic()
    scopes = await scopes_for(db, actx)
    await ensure_fresh_prices(db, scopes)
    key = cache.make_key(f"search:{actx.channel}:{actx.mode}", [s.business_id for s in scopes], query.applied()
                         | {"page": query.page, "page_size": query.page_size, "ids": [str(i) for i in query.product_ids or []]})
    data = cache.get(key)
    cached = data is not None
    if data is None:
        data = await get_backend().search(db, scopes, actx.channel, query)
        cache.put(key, data, data.get("valid_until"))
    per: dict = {}
    by_slug = {s.profile.slug: s.business_id for s in scopes}
    for r in data["items"]:
        bid = by_slug.get(r["product"]["merchant"]["id"])
        per[bid] = per.get(bid, 0) + 1
    if not per and scopes and actx.mode != "public":
        per = {scopes[0].business_id: 0}
    await _record(db, actx, scopes, kind, started, category=query.category, per_merchant=per,
                  meta={"q": query.q, "total": data["total"], "cached": cached,
                        # recorded so Commerce Intelligence can report filters/attributes agents ask for
                        "filters": sorted(k for k in query.applied() if k not in ("q", "sort")),
                        "attributes": [a.key for a in query.attributes], "variant_options": [a.key for a in query.variant_options],
                        "attribute_filters": [a.as_text() for a in query.attributes][:10]})
    return _envelope({**data, "cached": cached}, scopes)


async def _find(db: AsyncSession, actx: AgentContext, scopes: list[MerchantScope], product_id: str):
    try:
        pid = uuid.UUID(str(product_id))
    except ValueError:
        raise AgentError(404, "not_found", "Product not found")
    for sc in scopes:
        p = (await db.execute(select(Product).where(eligibility_clause(sc, actx.channel), Product.id == pid))).scalar_one_or_none()
        if p is not None:
            return p, sc
    # deliberately the same message whether the product does not exist, is private or belongs to someone else
    raise AgentError(404, "not_found", "Product not found")


async def product(db: AsyncSession, actx: AgentContext, product_id: str, kind: str = "product_detail") -> dict:
    started = time.monotonic()
    scopes = await scopes_for(db, actx)
    await ensure_fresh_prices(db, scopes)
    key = cache.make_key(f"product:{actx.channel}:{actx.mode}", [s.business_id for s in scopes], {"id": str(product_id)})
    data = cache.get(key)
    if data is None:
        p, sc = await _find(db, actx, scopes, product_id)
        data = public_product(p, sc)
        cache.put(key, data, (data.get("price") or {}).get("valid_until"))
        bid = sc.business_id
    else:
        bid = next((s.business_id for s in scopes if s.profile.slug == data["merchant"]["id"]), scopes[0].business_id)
    await _record(db, actx, scopes, kind, started, product_id=uuid.UUID(data["id"]),
                  category=(data.get("category") or {}).get("path"), per_merchant={bid: 1})
    return data


async def product_detail(db, actx, product_id) -> dict:
    return _envelope({"product": await product(db, actx, product_id)}, [])


async def variants(db, actx, product_id) -> dict:
    data = await product(db, actx, product_id, "variants")
    return _envelope({"product_id": data["id"], "name": data["name"], "variants": data["variants"],
                      "has_variants": bool(data["variants"])}, [])


async def availability(db, actx, product_id) -> dict:
    data = await product(db, actx, product_id, "availability")
    return _envelope({"product_id": data["id"], "name": data["name"], "availability": data["availability"],
                      "variants": [{"id": v["id"], "name": v["name"], "sku": v["sku"], "options": v["options"],
                                    "availability": v["availability"]} for v in data["variants"]],
                      "note": "Stock quantities are never shared; statuses are derived from the merchant's live inventory "
                              "when it is tracked, otherwise from the website or merchant-entered availability.",
                      "updated_at": data["updated_at"]}, [])


async def pricing(db, actx, product_id) -> dict:
    data = await product(db, actx, product_id, "pricing")
    upcoming = None
    if data.get("price") is not None:
        nxt = (await db.execute(select(ScheduledPriceChange.effective_at).where(
            ScheduledPriceChange.product_id == uuid.UUID(data["id"]), ScheduledPriceChange.status == "scheduled")
            .order_by(ScheduledPriceChange.effective_at).limit(1))).scalar_one_or_none()
        if nxt is not None:
            upcoming = {"price_change_scheduled_at": nxt.isoformat()}
    return _envelope({"product_id": data["id"], "name": data["name"], "price": data["price"],
                      "price_visibility": "withheld" if "price" in data["withheld_fields"] else (
                          "unknown" if data["price"] is None else "public"),
                      "variants": [{"id": v["id"], "name": v["name"], "sku": v["sku"], "price": v["price"]} for v in data["variants"]],
                      "scheduled": upcoming, "updated_at": data["updated_at"],
                      "note": "Prices are in the merchant's currency and never converted. Cost prices are never shared."}, [])


async def compare(db, actx, product_ids: list[str]) -> dict:
    started = time.monotonic()
    ids = list(dict.fromkeys(str(i) for i in product_ids))
    if not 2 <= len(ids) <= 10:
        raise AgentError(422, "validation_error", "Compare between 2 and 10 different products")
    scopes = await scopes_for(db, actx)
    await ensure_fresh_prices(db, scopes)
    pubs, missing, per = [], [], {}
    for pid in ids:
        try:
            p, sc = await _find(db, actx, scopes, pid)
        except AgentError:
            missing.append(pid)
            continue
        pubs.append(public_product(p, sc))
        per[sc.business_id] = per.get(sc.business_id, 0) + 1
    if missing:
        raise AgentError(404, "not_found", "Some products were not found", {"missing": missing})
    # one event per compared product (per merchant), so comparison interest is attributable to products
    ms = int((time.monotonic() - started) * 1000)
    by_slug = {s.profile.slug: s.business_id for s in scopes}
    for pub in pubs:
        analytics.record(db, by_slug[pub["merchant"]["id"]], "mcp_call" if actx.channel == "mcp" else "compare", actx.channel,
                         actx.consumer, product_id=uuid.UUID(pub["id"]), category=(pub.get("category") or {}).get("path"),
                         latency_ms=ms, result_count=len(pubs),
                         meta={"products": len(pubs), **({"tool": "compare"} if actx.channel == "mcp" else {})})
    await db.commit()
    return _envelope({"comparison": compare_products(pubs)}, scopes)


async def merchants(db, actx, q: str | None = None) -> dict:
    started = time.monotonic()
    scopes = await scopes_for(db, actx)
    out = []
    for sc in scopes:
        if q and q.lower() not in (sc.merchant_ref()["name"] or "").lower() and q.lower() not in sc.profile.slug:
            continue
        rows = (await db.execute(select(Product.currency, Product.category_id, Product.updated_at).where(
            eligibility_clause(sc, actx.channel)))).all()
        out.append({**sc.merchant_ref(), "product_count": len(rows),
                    "currencies": sorted({c for c, _, _ in rows if c}),
                    "category_count": len({cid for _, cid, _ in rows if cid}),
                    "last_updated": max((u for _, _, u in rows if u), default=None),
                    "policies": {"returns": sc.profile.return_policy_url, "privacy": sc.profile.privacy_policy_url,
                                 "terms": sc.profile.terms_url},
                    "checkout": "merchant_redirect" if sc.profile.checkout_enabled else "not_offered"})
    for m in out:
        m["last_updated"] = m["last_updated"].isoformat() if m["last_updated"] else None
    await _record(db, actx, scopes, "merchants", started, per_merchant={s.business_id: 1 for s in scopes
                                                                         if any(m["id"] == s.profile.slug for m in out)})
    return _envelope({"items": out, "total": len(out)}, scopes)


async def categories(db, actx, merchant: str | None = None) -> dict:
    started = time.monotonic()
    scopes = await scopes_for(db, actx)
    if merchant:
        scopes = [s for s in scopes if s.profile.slug == merchant]
    items = []
    for sc in scopes:
        counts = dict((await db.execute(select(Product.category_id, func.count()).where(
            eligibility_clause(sc, actx.channel)).group_by(Product.category_id))).all())
        cats = (await db.execute(select(Category).where(Category.business_id == sc.business_id,
                                                        Category.archived_at.is_(None)).order_by(Category.path_key))).scalars().all()
        for c in cats:
            if c.id in sc.excluded_categories:
                continue
            items.append({"id": str(c.id), "name": c.name, "path": c.path, "merchant": sc.profile.slug,
                          "parent_id": str(c.parent_id) if c.parent_id else None, "product_count": counts.get(c.id, 0)})
    await _record(db, actx, scopes, "categories", started, per_merchant={s.business_id: None for s in scopes})
    return _envelope({"items": items, "total": len(items)}, scopes)


async def merchant_catalog(db, actx, merchant: str, page: int = 1, page_size: int = 50) -> dict:
    q = SearchQuery(merchants=[merchant], sort="name", page=page, page_size=page_size)
    scopes = await scopes_for(db, actx)
    if not any(s.profile.slug == merchant for s in scopes):
        raise AgentError(404, "not_found", "Merchant not found")
    return await search(db, actx, q, kind="catalog")


async def recommend(db, actx, text: str, base: SearchQuery, interpret: str = "deterministic", limit: int = 5) -> dict:
    """Deterministic matching first; Groq only when interpret='ai' (it produces filters, never results)."""
    from app.services.distribution import nlquery

    scopes = await scopes_for(db, actx)
    brands = []
    for sc in scopes:
        brands += [b for (b,) in (await db.execute(select(Product.brand).where(
            eligibility_clause(sc, actx.channel), Product.brand.is_not(None)).distinct().limit(500))).all()]
    interpretation: dict
    if interpret == "ai":
        bid = actx.business.id if actx.business else None
        from app.core.ratelimit import hit

        if not await hit(f"agent-ai:{actx.api_key_id or actx.mode}", 10):
            raise AgentError(429, "rate_limited", "AI interpretation is limited to 10 requests per minute")
        try:
            q, meta = await nlquery.interpret_with_groq(text, db, bid)
            interpretation = {**meta, "filters": q.applied()}
        except SearchError as e:
            raise AgentError(422, "interpretation_failed", str(e))
        except Exception as e:  # provider errors are reported, never replaced with invented results
            raise AgentError(502, "ai_unavailable", f"AI interpretation failed: {type(e).__name__}")
    else:
        q, notes = nlquery.parse(text, brands)
        interpretation = {"source": "deterministic", "understood": notes, "filters": q.applied()}
    # explicit constraints from the caller always win over the interpretation
    for f in ("brand", "category", "min_price", "max_price", "currency"):
        if getattr(base, f) is not None:
            setattr(q, f, getattr(base, f))
    if base.availability:
        q.availability = base.availability
    q.attributes += base.attributes
    q.variant_options += base.variant_options
    q.merchants = base.merchants
    q.page, q.page_size, q.sort = 1, limit, "relevance"
    try:
        res = await search(db, actx, q, kind="recommend")
    except SearchError:
        raise
    return {**res, "interpretation": interpretation,
            "explanation": "Results are approved, distributed products that satisfy every filter, ordered by the published "
                           "ranking signals. No product, price or specification is generated."}
