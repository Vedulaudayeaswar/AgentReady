"""Dashboard metrics — computed from the database on every request."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.exports import export_dict
from app.api.v1.scans import scan_dict
from app.db.session import get_db
from app.models import ExportJob, Product, ScanJob, ScanProductSnapshot, ValidationIssue, Website

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview")
async def overview(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    bid = ctx.business_id
    active = (Product.business_id == bid, Product.is_removed.is_(False), Product.deleted_at.is_(None))
    by_quality = dict((await db.execute(select(Product.quality_status, func.count()).where(*active)
                                        .group_by(Product.quality_status))).all())
    by_approval = dict((await db.execute(select(Product.approval_status, func.count()).where(*active)
                                         .group_by(Product.approval_status))).all())
    total = sum(by_quality.values())
    avg_score = (await db.execute(select(func.avg(Product.quality_score)).where(*active))).scalar_one()
    by_category = (await db.execute(select(Product.category, func.count()).where(*active)
                                    .group_by(Product.category).order_by(func.count().desc()).limit(8))).all()
    open_issues = dict((await db.execute(select(ValidationIssue.severity, func.count())
                                         .join(Product, Product.id == ValidationIssue.product_id)
                                         .where(ValidationIssue.business_id == bid, ValidationIssue.status == "open", *active)
                                         .group_by(ValidationIssue.severity))).all())
    latest_scan = (await db.execute(select(ScanJob).where(ScanJob.business_id == bid)
                                    .order_by(ScanJob.created_at.desc()).limit(1))).scalar_one_or_none()
    latest_site = await db.get(Website, latest_scan.website_id) if latest_scan else None
    recent_changes = (await db.execute(
        select(ScanProductSnapshot, ScanJob.created_at).join(ScanJob, ScanJob.id == ScanProductSnapshot.scan_id)
        .where(ScanProductSnapshot.business_id == bid, ScanJob.is_rescan.is_(True),
               ScanProductSnapshot.change_type.in_(("new", "changed", "removed")))
        .order_by(ScanJob.created_at.desc()).limit(8)
    )).all()
    exports = (await db.execute(select(ExportJob).where(ExportJob.business_id == bid)
                                .order_by(ExportJob.created_at.desc()).limit(5))).scalars().all()
    websites = (await db.execute(select(func.count()).select_from(Website).where(Website.business_id == bid))).scalar_one()
    return {
        "total_products": total,
        "quality": {k: by_quality.get(k, 0) for k in ("valid", "needs_review", "incomplete", "duplicate_candidate", "invalid")},
        "approval": {k: by_approval.get(k, 0) for k in ("approved", "needs_review", "rejected")},
        "catalog_completeness": round(float(avg_score), 1) if avg_score is not None else None,
        "open_issues": {k: open_issues.get(k, 0) for k in ("error", "warning", "info")},
        "categories": [{"category": c or "Uncategorised", "count": n} for c, n in by_category],
        "latest_scan": scan_dict(latest_scan, latest_site) if latest_scan else None,
        "recent_changes": [{
            "id": str(s.id), "scan_id": str(s.scan_id), "change_type": s.change_type, "decision": s.decision,
            "name": (s.data or {}).get("name") or (s.changes[0].get("old") if s.changes else None),
            "fields": [c["field"] for c in s.changes or []][:6], "at": at.isoformat(),
        } for s, at in recent_changes],
        "recent_exports": [export_dict(e) for e in exports],
        "websites": websites,
        "commerce": await _commerce(db, bid, active),
    }


async def _commerce(db: AsyncSession, bid, active) -> dict:
    from app.models import BulkOperation, ScheduledPriceChange, SyncConflict, SyncJob

    def grouped(col):
        return select(col, func.count()).where(*active).group_by(col)

    lifecycle = dict((await db.execute(grouped(Product.lifecycle_status))).all())
    publication = dict((await db.execute(grouped(Product.publication_status).where(Product.lifecycle_status != "archived"))).all())
    stock = dict((await db.execute(grouped(Product.stock_status).where(Product.lifecycle_status != "archived"))).all())
    incomplete = (await db.execute(select(func.count()).select_from(Product).where(
        *active, Product.quality_status.in_(("incomplete", "invalid"))))).scalar_one()
    pending_changes = (await db.execute(select(func.count()).select_from(ScanProductSnapshot).join(
        ScanJob, ScanJob.id == ScanProductSnapshot.scan_id).where(
        ScanJob.business_id == bid, ScanJob.status == "awaiting_review", ScanProductSnapshot.decision == "pending",
        ScanProductSnapshot.change_type != "unchanged"))).scalar_one()
    open_conflicts = (await db.execute(select(func.count()).select_from(SyncConflict).where(
        SyncConflict.business_id == bid, SyncConflict.status == "open"))).scalar_one()
    scheduled = (await db.execute(select(func.count()).select_from(ScheduledPriceChange).where(
        ScheduledPriceChange.business_id == bid, ScheduledPriceChange.status == "scheduled"))).scalar_one()
    running_syncs = (await db.execute(select(func.count()).select_from(SyncJob).where(
        SyncJob.business_id == bid, SyncJob.status.in_(("queued", "running"))))).scalar_one()
    running_bulk = (await db.execute(select(func.count()).select_from(BulkOperation).where(
        BulkOperation.business_id == bid, BulkOperation.status.in_(("queued", "running"))))).scalar_one()
    approved = dict((await db.execute(grouped(Product.approval_status))).all()).get("approved", 0)
    return {
        "active": lifecycle.get("active", 0), "draft": lifecycle.get("draft", 0), "archived": lifecycle.get("archived", 0),
        "discontinued": lifecycle.get("discontinued", 0), "approved": approved,
        "published": publication.get("published", 0),
        "low_stock": stock.get("low_stock", 0), "out_of_stock": stock.get("out_of_stock", 0) + stock.get("backorder", 0),
        "stock": {k: stock.get(k, 0) for k in ("in_stock", "low_stock", "out_of_stock", "backorder", "untracked")},
        "incomplete": incomplete, "pending_website_changes": pending_changes,
        "pending_sync_operations": open_conflicts + scheduled + running_syncs + running_bulk,
        "pending": {"open_conflicts": open_conflicts, "scheduled_price_changes": scheduled, "running_syncs": running_syncs,
                    "running_bulk_operations": running_bulk},
    }


def _day(dt) -> str:
    from app.services.commerce.money import aware

    return aware(dt).date().isoformat()


@router.get("/charts")
async def charts(days: int = 30, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Chart series computed from stored records (no estimates)."""
    from datetime import timedelta

    from app.models import PriceHistory, SyncJob
    from app.services.commerce.money import now_utc

    days = max(7, min(days, 365))
    bid = ctx.business_id
    start = (now_utc() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    labels = [(start + timedelta(days=i)).date().isoformat() for i in range(days)]
    active = (Product.business_id == bid, Product.deleted_at.is_(None))
    created = (await db.execute(select(Product.created_at).where(*active))).scalars().all()
    before = sum(1 for c in created if _day(c) < labels[0])
    per_day: dict[str, int] = {}
    for c in created:
        if _day(c) >= labels[0]:
            per_day[_day(c)] = per_day.get(_day(c), 0) + 1
    growth, running = [], before
    for d in labels:
        running += per_day.get(d, 0)
        growth.append({"day": d, "products": running, "added": per_day.get(d, 0)})
    price_rows = (await db.execute(select(PriceHistory.created_at, PriceHistory.change_type).where(
        PriceHistory.business_id == bid, PriceHistory.created_at >= start))).all()
    price_days: dict[str, dict] = {d: {"day": d, "manual": 0, "bulk": 0, "scheduled": 0, "sync": 0} for d in labels}
    for at, ct in price_rows:
        key = "manual" if ct in ("manual", "product_created") else "bulk" if ct == "bulk" else "scheduled" if ct == "scheduled" else "sync"
        if _day(at) in price_days:
            price_days[_day(at)][key] += 1
    sync_rows = (await db.execute(select(SyncJob.created_at, SyncJob.status).where(
        SyncJob.business_id == bid, SyncJob.created_at >= start))).all()
    sync_days: dict[str, dict] = {d: {"day": d, "succeeded": 0, "failed": 0, "review": 0} for d in labels}
    for at, st in sync_rows:
        key = "failed" if st == "failed" else "review" if st == "awaiting_review" else "succeeded" if st.startswith("completed") else None
        if key and _day(at) in sync_days:
            sync_days[_day(at)][key] += 1
    live = (Product.business_id == bid, Product.is_removed.is_(False), Product.deleted_at.is_(None))
    by_category = (await db.execute(select(Product.category, func.count()).where(*live).group_by(Product.category)
                                    .order_by(func.count().desc()).limit(10))).all()
    stock = dict((await db.execute(select(Product.stock_status, func.count()).where(*live, Product.lifecycle_status != "archived")
                                   .group_by(Product.stock_status))).all())
    return {
        "days": days,
        "catalog_growth": growth,
        "products_by_category": [{"category": c or "Uncategorised", "count": n} for c, n in by_category],
        "inventory_status": [{"status": k, "count": stock.get(k, 0)} for k in ("in_stock", "low_stock", "out_of_stock", "backorder", "untracked")],
        "price_changes": list(price_days.values()),
        "sync_activity": list(sync_days.values()),
    }


@router.get("/activity")
async def activity(limit: int = 25, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    from app.api.v1.commerce import audit_dict
    from app.models import AuditLog

    rows = (await db.execute(select(AuditLog).where(AuditLog.business_id == ctx.business_id)
                             .order_by(AuditLog.created_at.desc()).limit(max(1, min(limit, 100))))).scalars().all()
    return {"items": [audit_dict(a) for a in rows]}
