from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.products import filtered_products
from app.core.errors import AppError, not_found
from app.db.session import get_db
from app.models import ExportJob, Product
from app.schemas.api import ExportIn
from app.services.export.exporters import FORMATS, render

router = APIRouter(prefix="/exports", tags=["exports"])


def export_dict(e: ExportJob) -> dict:
    return {"id": str(e.id), "format": e.format, "status": e.status, "product_count": e.product_count,
            "filters": e.filters, "filename": e.filename, "size_bytes": e.size_bytes, "error": e.error,
            "created_at": e.created_at.isoformat() if e.created_at else None}


@router.post("", status_code=201)
async def create_export(body: ExportIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    stmt = filtered_products(ctx.business_id, category=body.category,
                             approval_status=None if body.include_unapproved else "approved")
    stmt = stmt.where(Product.approval_status != "rejected")
    if body.product_ids:
        try:
            ids = [uuid.UUID(i) for i in body.product_ids]
        except ValueError:
            raise AppError(422, "invalid_id", "Invalid product id")
        stmt = stmt.where(Product.id.in_(ids))
    products = (await db.execute(stmt.order_by(Product.name))).scalars().all()
    if not products:
        raise AppError(422, "no_products", "No products match this export. Approve products first, or include unapproved ones.")
    content = render(body.format, list(products), ctx.business)
    ctype, ext = FORMATS[body.format]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job = ExportJob(business_id=ctx.business_id, format=body.format, status="completed", product_count=len(products),
                    filters=body.model_dump(exclude={"format"}), filename=f"agentready-catalog-{stamp}.{ext}",
                    content_type=ctype, content=content, size_bytes=len(content.encode()))
    db.add(job)
    await db.commit()
    return export_dict(job)


@router.get("")
async def list_exports(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                       ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    q = select(ExportJob).where(ExportJob.business_id == ctx.business_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ExportJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [export_dict(e) for e in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{export_id}/download")
async def download_export(export_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    try:
        eid = uuid.UUID(export_id)
    except ValueError:
        raise not_found("Export")
    e = (await db.execute(select(ExportJob).where(ExportJob.id == eid, ExportJob.business_id == ctx.business_id))).scalar_one_or_none()
    if e is None or e.content is None:
        raise not_found("Export")
    return Response(content=e.content, media_type=e.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{e.filename}"',
                             "X-Content-Type-Options": "nosniff"})
