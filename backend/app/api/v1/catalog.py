"""Published merchant catalog API (read-only).

Only approved, non-removed products are exposed. Authenticate with a merchant
session token or a catalog API key (``X-Catalog-Key``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, catalog_auth
from app.api.v1.products import contains_pattern
from app.core.errors import not_found
from app.db.session import get_db
from app.models import Product
from app.services.export.exporters import FEED_VERSION, feed_item, jsonld_product

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _public_key(key: str | None) -> bool:
    from app.services.distribution.public import is_private_attribute_key

    return not is_private_attribute_key(key)


def _published(business_id: uuid.UUID):
    from app.api.v2.catalog import public_filter

    return select(Product).where(public_filter(business_id))


@router.get("")
async def catalog(
    q: str | None = None, category: str | None = None, format: str = Query("feed", pattern="^(feed|jsonld)$"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db),
):
    from app.api.v2.catalog import require

    require(ctx, "catalog:read")
    stmt = _published(ctx.business_id)
    if q:
        stmt = stmt.where(func.lower(Product.name).like(contains_pattern(q), escape="\\"))
    if category:
        stmt = stmt.where(Product.category == category)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Product.name, Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = [jsonld_product(p, ctx.business, keep_attribute=_public_key) if format == "jsonld"
             else feed_item(p, keep_attribute=_public_key) for p in rows]
    return {"feed_version": FEED_VERSION, "merchant": {"id": str(ctx.business.id), "name": ctx.business.name},
            "items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{product_id}")
async def catalog_item(product_id: str, format: str = Query("feed", pattern="^(feed|jsonld)$"),
                       ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    try:
        pid = uuid.UUID(product_id)
    except ValueError:
        raise not_found("Product")
    p = (await db.execute(_published(ctx.business_id).where(Product.id == pid))).scalar_one_or_none()
    if p is None:
        raise not_found("Product")
    if format == "jsonld":
        return {"@context": "https://schema.org", **jsonld_product(p, ctx.business, keep_attribute=_public_key)}
    return feed_item(p, keep_attribute=_public_key)
