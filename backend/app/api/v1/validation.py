from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.core.errors import not_found
from app.db.session import get_db
from app.models import Product, ValidationIssue
from app.schemas.api import ResolveIssueIn
from app.services.validation.validator import quality_status

router = APIRouter(prefix="/validation", tags=["validation"])


def issue_dict(i: ValidationIssue, product: Product | None = None) -> dict:
    d = {
        "id": str(i.id), "product_id": str(i.product_id), "code": i.code, "severity": i.severity, "field": i.field,
        "message": i.message, "details": i.details or {}, "status": i.status, "resolution_note": i.resolution_note,
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }
    if product is not None:
        d["product"] = {"id": str(product.id), "name": product.name, "sku": product.sku,
                        "quality_status": product.quality_status, "source_url": product.source_url}
    return d


@router.get("/issues")
async def list_issues(
    status: str = "open", severity: str | None = None, code: str | None = None, product_id: str | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db),
):
    q = (select(ValidationIssue, Product).join(Product, Product.id == ValidationIssue.product_id)
         .where(ValidationIssue.business_id == ctx.business_id, Product.business_id == ctx.business_id,
                Product.is_removed.is_(False)))
    if status != "all":
        q = q.where(ValidationIssue.status == status)
    if severity:
        q = q.where(ValidationIssue.severity.in_(severity.split(",")))
    if code:
        q = q.where(ValidationIssue.code.in_(code.split(",")))
    if product_id:
        try:
            q = q.where(ValidationIssue.product_id == uuid.UUID(product_id))
        except ValueError:
            raise not_found("Product")
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    sev_order = case((ValidationIssue.severity == "error", 0), (ValidationIssue.severity == "warning", 1), else_=2)
    rows = (await db.execute(q.order_by(sev_order, ValidationIssue.code, ValidationIssue.created_at)
                             .offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [issue_dict(i, p) for i, p in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/summary")
async def summary(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ValidationIssue.code, ValidationIssue.severity, func.count())
        .join(Product, Product.id == ValidationIssue.product_id)
        .where(ValidationIssue.business_id == ctx.business_id, ValidationIssue.status == "open", Product.is_removed.is_(False))
        .group_by(ValidationIssue.code, ValidationIssue.severity)
    )).all()
    return {"items": [{"code": c, "severity": s, "count": n} for c, s, n in sorted(rows, key=lambda r: -r[2])]}


@router.post("/issues/{issue_id}/resolve")
async def resolve_issue(issue_id: str, body: ResolveIssueIn, ctx: AuthContext = Depends(current_auth),
                        db: AsyncSession = Depends(get_db)):
    try:
        iid = uuid.UUID(issue_id)
    except ValueError:
        raise not_found("Issue")
    issue = (await db.execute(select(ValidationIssue).where(
        ValidationIssue.id == iid, ValidationIssue.business_id == ctx.business_id))).scalar_one_or_none()
    if issue is None:
        raise not_found("Issue")
    now = datetime.now(timezone.utc)
    if body.action == "reopen":
        issue.status, issue.resolved_at, issue.resolution_note = "open", None, None
    else:
        issue.status = "resolved" if body.action == "resolve" else "dismissed"
        issue.resolved_at = now
        issue.resolution_note = body.note
        issue.details = {**(issue.details or {}), "auto_resolved": False}
    await db.flush()
    product = await db.get(Product, issue.product_id)
    if product is not None:
        open_issues = (await db.execute(select(ValidationIssue).where(
            ValidationIssue.product_id == product.id, ValidationIssue.status == "open"))).scalars().all()
        product.quality_status = quality_status(list(open_issues))
    await db.commit()
    return issue_dict(issue, product)
