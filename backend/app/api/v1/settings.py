from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.auth import _business_out
from app.core.config import get_settings
from app.core.security import generate_opaque_token, hash_opaque_token
from app.db.session import get_db
from app.models import AIRequest, Product
from app.schemas.api import BusinessOut, BusinessUpdate
from app.services.ai.base import AIConfigurationError
from app.services.ai.gateway import build_provider

router = APIRouter(tags=["settings"])


@router.get("/business", response_model=BusinessOut)
async def get_business(ctx: AuthContext = Depends(current_auth)):
    return _business_out(ctx.business)


@router.patch("/business", response_model=BusinessOut)
async def update_business(body: BusinessUpdate, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    b = ctx.business
    for f in body.model_fields_set:
        v = getattr(body, f)
        if f in ("default_currency", "country") and v:
            v = v.upper()
        setattr(b, f, v)
    await db.commit()
    return _business_out(b)


@router.post("/business/catalog-key")
async def rotate_catalog_key(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Create or rotate the read-only catalog API key. The key is shown once."""
    from app.api.v1.commerce import issue_key
    from app.models import ApiKey
    from app.services.commerce.audit import actor_from_ctx, record
    from app.services.commerce.money import now_utc

    old = (await db.execute(select(ApiKey).where(ApiKey.business_id == ctx.business_id, ApiKey.is_legacy_default.is_(True),
                                                 ApiKey.revoked_at.is_(None)))).scalars().all()
    for k in old:
        k.revoked_at = now_utc()
    k, key = await issue_key(db, ctx, "Default catalog key", ["catalog:read", "pricing:read", "inventory:read"], 120,
                             rotated_from=old[0] if old else None, legacy=True)
    ctx.business.catalog_api_key_hash = None  # keys live in api_keys now
    ctx.business.catalog_api_key_prefix = key[:12]
    record(db, ctx.business_id, actor_from_ctx(ctx), "api_key.rotated" if old else "api_key.created", "api_key", k.id,
           f"Default catalog key {'rotated' if old else 'created'} ({key[:12]}…)", {"prefix": key[:12]})
    await db.commit()
    return {"catalog_api_key": key, "prefix": key[:12],
            "usage": "Send as the X-Catalog-Key header to GET /api/v1/catalog or /api/v2/catalog/products"}


@router.get("/settings/ai")
async def ai_settings(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    rows = (await db.execute(
        select(
            AIRequest.status, AIRequest.model, func.count(), func.sum(AIRequest.prompt_tokens),
            func.sum(AIRequest.completion_tokens), func.sum(AIRequest.estimated_cost_usd), func.avg(AIRequest.latency_ms),
        ).where(AIRequest.business_id == ctx.business_id).group_by(AIRequest.status, AIRequest.model)
    )).all()
    by_model: dict[str, dict] = {}
    for status, model, count, pt, ct, cost, lat in rows:
        m = by_model.setdefault(model, {"model": model, "requests": 0, "cached": 0, "errors": 0, "prompt_tokens": 0,
                                        "completion_tokens": 0, "estimated_cost_usd": 0.0, "avg_latency_ms": None})
        m["requests"] += count
        m["cached"] += count if status == "cached" else 0
        m["errors"] += count if status == "error" else 0
        m["prompt_tokens"] += int(pt or 0)
        m["completion_tokens"] += int(ct or 0)
        m["estimated_cost_usd"] = round(m["estimated_cost_usd"] + float(cost or 0), 6)
        if status == "success" and lat is not None:
            m["avg_latency_ms"] = int(lat)
    products = (await db.execute(
        select(func.count()).select_from(Product).where(Product.business_id == ctx.business_id, Product.is_removed.is_(False),
                                                         Product.deleted_at.is_(None))
    )).scalar_one()
    total_tokens = sum(m["prompt_tokens"] + m["completion_tokens"] for m in by_model.values())
    return {
        "provider": s.ai_provider,
        "configured": s.ai_enabled,
        "is_mock": s.ai_provider == "mock",
        "model": s.groq_model if s.ai_provider == "groq" else ("mock-local" if s.ai_provider == "mock" else None),
        "extraction_mode": s.ai_extraction_mode,
        "max_ai_calls_per_scan": s.max_ai_calls_per_scan,
        "pricing_usd_per_mtok": {"input": s.groq_input_price_per_mtok, "output": s.groq_output_price_per_mtok},
        "usage": list(by_model.values()),
        "tokens_per_100_products": round(total_tokens / products * 100) if products else None,
    }


@router.post("/settings/ai/verify")
async def verify_ai(ctx: AuthContext = Depends(current_auth)):
    """Check that the configured Groq model is available to the API key."""
    provider = build_provider()
    if provider is None:
        return {"ok": False, "message": "No AI provider configured (set GROQ_API_KEY)."}
    try:
        info = await provider.verify_model()
        return {"ok": bool(info.get("available")), **info}
    except AIConfigurationError as e:
        return {"ok": False, "message": str(e)}
    finally:
        await provider.aclose()
