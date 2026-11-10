"""Agent Product Discovery API (``/api/v3/agents``) — for external AI agents and applications.

Authentication (all optional except for carts/checkout):
* ``X-Catalog-Key: <key>`` or ``Authorization: Bearer <key>`` — API key with ``agent:read``: that merchant's catalog.
* No credentials — the public network: merchants that opted in. Rate limited per IP.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_SCOPES, bearer, current_auth, verify_api_key
from app.core.config import get_settings
from app.core.ratelimit import hit
from app.db.session import get_db
from app.models import Product
from app.schemas.distribution import CartIn, CheckoutIn, CompareIn, RecommendIn
from app.services.commerce.money import aware, now_utc
from app.services.distribution import agent, checkout, tools
from app.services.distribution.agent import AgentContext, AgentError
from app.services.distribution.public import SCHEMA, eligibility_clause, public_product, removal_reason
from app.services.distribution.search import RANKING_POLICY, AttrFilter, SearchError, SearchQuery

router = APIRouter(prefix="/agents", tags=["agent API v3"])
ANON_LIMIT_PER_MINUTE = 60


async def resolve_agent(request: Request, creds: HTTPAuthorizationCredentials | None, x_key: str | None,
                        db: AsyncSession, channel: str) -> AgentContext:
    raw = x_key or (creds.credentials if creds and creds.credentials.startswith("ar_") else None)
    if raw:
        key, business = await verify_api_key(db, raw)
        if "agent:read" not in (key.scopes or []):
            raise HTTPException(403, "This API key lacks the 'agent:read' scope")
        return AgentContext("api_key", channel, business, str(key.id), key.name, list(key.scopes or []))
    if creds is not None:
        ctx = await current_auth(creds, db)
        return AgentContext("merchant", channel, ctx.business, None, ctx.user.email if ctx.user else None, list(ALL_SCOPES))
    ip = request.client.host if request.client else "unknown"
    if not await hit(f"agent-anon:{ip}", ANON_LIMIT_PER_MINUTE):
        raise HTTPException(429, "Too many requests from this address; use an API key for higher limits",
                            headers={"Retry-After": "60"})
    ua = (request.headers.get("user-agent") or "").split(" ")[0][:60]
    return AgentContext("public", channel, None, None, f"Anonymous ({ua})" if ua else None, [])


async def agent_ctx(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                    x_catalog_key: str | None = Header(default=None, alias="X-Catalog-Key"),
                    db: AsyncSession = Depends(get_db)) -> AgentContext:
    return await resolve_agent(request, creds, x_catalog_key, db, "agent_api")


async def _run(db, actx: AgentContext, kind: str, coro):
    try:
        return await coro
    except AgentError as e:
        await agent.record_error(db, actx, kind, e.status, e.message)
        raise
    except SearchError as e:
        await agent.record_error(db, actx, kind, 422, str(e))
        raise AgentError(422, "invalid_query", str(e))


def _filters(values: list[str]) -> list[AttrFilter]:
    try:
        return [AttrFilter.parse(v) for v in values]
    except SearchError as e:
        raise AgentError(422, "invalid_query", str(e))


def build_query(q, brand, sku, gtin, category, min_price, max_price, currency, availability, merchant, attr, variant,
                updated_since, sort, page, page_size) -> SearchQuery:
    return SearchQuery(q=q, brand=brand, sku=sku, gtin=gtin, category=category, min_price=min_price, max_price=max_price,
                       currency=currency.upper() if currency else None,
                       availability=[a for a in (availability or "").split(",") if a],
                       merchants=[m for m in (merchant or "").split(",") if m], attributes=_filters(attr),
                       variant_options=_filters(variant), updated_since=updated_since, sort=sort, page=page,
                       page_size=page_size)


def _search_params(
    q: str | None = Query(None, max_length=200, description="Keywords"),
    brand: str | None = Query(None, max_length=200), sku: str | None = Query(None, max_length=200),
    gtin: str | None = Query(None, max_length=20), category: str | None = Query(None, max_length=300),
    min_price: Decimal | None = Query(None, ge=0), max_price: Decimal | None = Query(None, ge=0),
    currency: str | None = Query(None, pattern="^[A-Za-z]{3}$"),
    availability: str | None = Query(None, description="Comma-separated: available,out_of_stock,preorder,backorder,discontinued,unknown"),
    merchant: str | None = Query(None, description="Merchant id(s), comma-separated"),
    attr: list[str] = Query(default_factory=list, description="Attribute filter, repeatable: voltage:230 V, storage>=512 GB"),
    variant: list[str] = Query(default_factory=list, description="Variant option filter, repeatable: size:M, color:black"),
    updated_since: datetime | None = None,
    sort: str = Query("relevance", pattern="^(relevance|price_asc|price_desc|name|updated)$"),
    page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100),
) -> SearchQuery:
    return build_query(q, brand, sku, gtin, category, min_price, max_price, currency, availability, merchant, attr, variant,
                       updated_since, sort, page, page_size)


# ------------------------------------------------------------------ discovery
def capability_document() -> dict:
    s = get_settings()
    base = s.api_base_url.rstrip("/")
    from app.api.v3.mcp import HANDSHAKE_VERSIONS, MODERN_VERSIONS

    return {
        "service": {"name": "AgentReady", "description": "Merchant-approved product catalogs for AI agents",
                    "api_version": agent.API_VERSION, "product_schema": SCHEMA},
        "authentication": {
            "public_network": "No credentials: catalogs of merchants that joined the public network (rate limited per IP)",
            "api_key": {"headers": ["X-Catalog-Key: <key>", "Authorization: Bearer <key>"], "scope": "agent:read",
                        "issued_by": "Each merchant, in AgentReady → Distribution → Agent API"},
            "checkout": "Carts and checkout hand-offs need an API key with checkout:write"},
        "rate_limits": {"anonymous_per_minute": ANON_LIMIT_PER_MINUTE,
                        "api_key_per_minute": f"per key (default {s.api_key_default_rate_limit})",
                        "ai_interpretation_per_minute": 10},
        "endpoints": {
            "search": f"{base}/api/v3/agents/search", "products": f"{base}/api/v3/agents/products",
            "product": f"{base}/api/v3/agents/products/{{product_id}}",
            "variants": f"{base}/api/v3/agents/products/{{product_id}}/variants",
            "availability": f"{base}/api/v3/agents/products/{{product_id}}/availability",
            "pricing": f"{base}/api/v3/agents/products/{{product_id}}/pricing",
            "compare": f"{base}/api/v3/agents/compare", "recommend": f"{base}/api/v3/agents/recommend",
            "categories": f"{base}/api/v3/agents/categories", "merchants": f"{base}/api/v3/agents/merchants",
            "changes": f"{base}/api/v3/agents/changes?since=<ISO-8601>",
            "tools": f"{base}/api/v3/agents/tools", "carts": f"{base}/api/v3/agents/carts",
        },
        "tools": [{"name": t.name, "title": t.title, "description": t.description} for t in tools.TOOLS.values()],
        "mcp": {"endpoint": f"{base}/api/v3/mcp", "transport": "Streamable HTTP (JSON responses)",
                "protocol_versions": MODERN_VERSIONS + HANDSHAKE_VERSIONS, "tools": "read-only"},
        "formats": {"api": "application/json", "feeds": ["json", "jsonl", "csv", "jsonld", "xml"],
                    "feed_profiles": ["agentready", "openai", "google"]},
        "ranking_policy": RANKING_POLICY,
        "data_policy": ["Only merchant-approved, published and distributed products are returned",
                        "Cost prices, stock quantities, reserved stock, locations, notes and evidence are never returned",
                        "Prices keep their original currency and are never converted",
                        "Missing values are listed in unknown_fields; values a merchant chose not to share in withheld_fields"],
        "documentation": ({"openapi": f"{base}{s.api_prefix}/openapi.json", "docs": f"{base}/docs"} if s.docs_enabled
                          else {"note": "Interactive API documentation is disabled on this deployment"}),
    }


@router.get("/capabilities")
async def capabilities():
    return capability_document()


@router.get("/tools")
async def list_tools():
    return {"tools": [t.definition() for t in tools.TOOLS.values()]}


@router.post("/tools/{name}")
async def call_tool(name: str, request: Request, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    try:
        args = await request.json()
    except ValueError:
        raise AgentError(422, "validation_error", "The body must be a JSON object of tool arguments")
    if not isinstance(args, dict):
        raise AgentError(422, "validation_error", "The body must be a JSON object of tool arguments")
    if name not in tools.TOOLS:
        raise AgentError(404, "unknown_tool", f"Unknown tool '{name}'")
    try:
        return {"tool": name, "result": await _run(db, actx, name, tools.call(db, actx, name, args))}
    except tools.ToolInputError as e:
        raise AgentError(422, "invalid_arguments", str(e), {"errors": e.details} if e.details else None)


@router.get("/search")
async def search(query: SearchQuery = Depends(_search_params), actx: AgentContext = Depends(agent_ctx),
                 db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "search", agent.search(db, actx, query))


@router.get("/products")
async def products(query: SearchQuery = Depends(_search_params), actx: AgentContext = Depends(agent_ctx),
                   db: AsyncSession = Depends(get_db)):
    if query.sort == "relevance" and not query.q:
        query.sort = "name"
    return await _run(db, actx, "catalog", agent.search(db, actx, query, kind="catalog"))


@router.get("/products/{product_id}")
async def product(product_id: str, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "product_detail", agent.product_detail(db, actx, product_id))


@router.get("/products/{product_id}/variants")
async def variants(product_id: str, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "variants", agent.variants(db, actx, product_id))


@router.get("/products/{product_id}/availability")
async def availability(product_id: str, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "availability", agent.availability(db, actx, product_id))


@router.get("/products/{product_id}/pricing")
async def pricing(product_id: str, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "pricing", agent.pricing(db, actx, product_id))


@router.post("/compare")
async def compare(body: CompareIn, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "compare", agent.compare(db, actx, body.product_ids))


@router.get("/compare")
async def compare_get(ids: str = Query(..., description="Comma-separated product ids"),
                      actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "compare", agent.compare(db, actx, [i for i in ids.split(",") if i]))


@router.post("/recommend")
async def recommend(body: RecommendIn, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    base = SearchQuery(min_price=Decimal(str(body.min_price)) if body.min_price is not None else None,
                       max_price=Decimal(str(body.max_price)) if body.max_price is not None else None,
                       currency=body.currency, category=body.category,
                       availability=["available"] if body.available_only else [],
                       merchants=[body.merchant] if body.merchant else [])
    return await _run(db, actx, "recommend", agent.recommend(db, actx, body.request, base, body.interpret, body.limit))


@router.get("/categories")
async def categories(merchant: str | None = None, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "categories", agent.categories(db, actx, merchant))


@router.get("/merchants")
async def merchants(q: str | None = Query(None, max_length=200), actx: AgentContext = Depends(agent_ctx),
                    db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "merchants", agent.merchants(db, actx, q))


@router.get("/merchants/{merchant}/products")
async def merchant_products(merchant: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                            actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "catalog", agent.merchant_catalog(db, actx, merchant, page, page_size))


@router.get("/changes")
async def changes(since: datetime, page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
                  actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    """Incremental sync: products changed since ``since`` plus tombstones for products that left distribution.
    Use ``next_since`` as the cursor for the next call."""
    started = time.monotonic()
    server_time = now_utc()
    since = aware(since)
    scopes = await _run(db, actx, "catalog", agent.scopes_for(db, actx))
    await agent.ensure_fresh_prices(db, scopes)
    items, removed = [], []
    for sc in scopes:
        rows = (await db.execute(select(Product).where(eligibility_clause(sc, "agent_api"), Product.updated_at > since)
                                 .order_by(Product.updated_at, Product.id))).scalars().all()
        items += [public_product(p, sc, server_time) for p in rows]
        gone = (await db.execute(select(Product).where(
            Product.business_id == sc.business_id, Product.published_at.is_not(None), Product.updated_at > since,
            ~eligibility_clause(sc, "agent_api")).limit(5000))).scalars().all()
        removed += [{"id": str(p.id), "removed": True, "reason": removal_reason(p),
                     "at": aware(p.updated_at).isoformat()} for p in gone]
    total = len(items)
    page_items = items[(page - 1) * page_size: page * page_size]
    await agent._record(db, actx, scopes, "catalog", started, result_count=len(page_items), meta={"changes_since": since.isoformat()})
    return {"schema": SCHEMA, "api_version": agent.API_VERSION, "since": since.isoformat(), "items": page_items,
            "removed": removed if page == 1 else [], "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total, "next_since": server_time.isoformat()}


# ------------------------------------------------------------------ carts & checkout hand-off
@router.post("/carts", status_code=201)
async def create_cart(body: CartIn, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "cart", checkout.create_cart(db, actx, [i.model_dump() for i in body.items]))


@router.get("/carts/{cart_id}")
async def get_cart(cart_id: str, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "cart", checkout.get_cart(db, actx, cart_id))


@router.post("/carts/{cart_id}/checkout", status_code=201)
async def create_checkout(cart_id: str, body: CheckoutIn, actx: AgentContext = Depends(agent_ctx),
                          db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "checkout_session", checkout.create_checkout(
        db, actx, cart_id, body.expected_subtotal, body.expected_currency))


@router.get("/checkout-sessions/{session_id}")
async def get_checkout(session_id: str, actx: AgentContext = Depends(agent_ctx), db: AsyncSession = Depends(get_db)):
    return await _run(db, actx, "checkout_session", checkout.get_session(db, actx, session_id))
