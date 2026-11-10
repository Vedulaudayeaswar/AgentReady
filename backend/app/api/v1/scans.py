from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.websites import get_owned_website
from app.core.config import get_settings
from app.core.errors import AppError, not_found
from app.core.ratelimit import rate_limit
from app.db.session import get_db
from app.models import (
    ACTIVE_SCAN_STATUSES,
    AIRequest,
    CrawledPage,
    Product,
    ProductEvidence,
    ScanJob,
    ScanProductSnapshot,
    Website,
)
from app.schemas.api import ApplyScanIn, ScanIn
from app.services.catalog.service import apply_scan
from app.workers.scan_runner import STAGES

router = APIRouter(prefix="/scans", tags=["scans"])
log = logging.getLogger("agentready.scans")


def scan_outcome(s: ScanJob) -> str:
    """completed | completed_with_warnings | the raw status for anything else."""
    if s.status != "completed":
        return s.status
    warned = s.pages_failed > 0 or any((e.get("level") or "error") in ("warning", "error") for e in s.errors or [])
    return "completed_with_warnings" if warned else "completed"


def scan_dict(s: ScanJob, website=None) -> dict:
    return {
        "outcome": scan_outcome(s),
        "id": str(s.id), "website_id": str(s.website_id),
        "website_url": website.normalized_url if website is not None else None,
        "status": s.status, "stage": s.stage, "is_rescan": s.is_rescan, "settings": s.settings,
        "pages_discovered": s.pages_discovered, "pages_crawled": s.pages_crawled, "pages_failed": s.pages_failed,
        "pages_skipped_robots": s.pages_skipped_robots, "product_pages_found": s.product_pages_found,
        "products_extracted": s.products_extracted, "ai_calls": s.ai_calls, "errors": s.errors or [],
        "change_summary": s.change_summary or {}, "stage_history": s.stage_history or [],
        "cancel_requested": s.cancel_requested,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        "applied_at": s.applied_at.isoformat() if s.applied_at else None,
    }


async def get_owned_scan(db: AsyncSession, ctx: AuthContext, scan_id: str) -> ScanJob:
    try:
        sid = uuid.UUID(scan_id)
    except ValueError:
        raise not_found("Scan")
    s = (await db.execute(select(ScanJob).where(ScanJob.id == sid, ScanJob.business_id == ctx.business_id))).scalar_one_or_none()
    if s is None:
        raise not_found("Scan")
    return s


from app.services.commerce.sync import fail_stale_scans  # noqa: E402,F401  (re-exported)


@router.post("", status_code=202, dependencies=[Depends(rate_limit("start_scan", limit=10))])
async def start_scan(body: ScanIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    from app.services.commerce.audit import actor_from_ctx
    from app.services.commerce.sync import create_scan, dispatch_or_fail

    website = await get_owned_website(db, ctx, body.website_id)
    scan, _ = await create_scan(db, ctx.business, website, trigger="manual", actor=actor_from_ctx(ctx),
                                overrides={"max_pages": body.max_pages, "max_depth": body.max_depth})
    backend = await dispatch_or_fail(db, scan)
    return {**scan_dict(scan, website), "job_backend": backend}


@router.get("")
async def list_scans(
    website_id: str | None = None, status: str | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db),
):
    q = select(ScanJob).where(ScanJob.business_id == ctx.business_id)
    if website_id:
        q = q.where(ScanJob.website_id == (await get_owned_website(db, ctx, website_id)).id)
    if status:
        q = q.where(ScanJob.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ScanJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    sites = {w.id: w for w in (await db.execute(select(Website).where(Website.business_id == ctx.business_id))).scalars()}
    return {"items": [scan_dict(s, sites.get(s.website_id)) for s in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{scan_id}")
async def get_scan(scan_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await get_owned_scan(db, ctx, scan_id)
    ai_rows = (await db.execute(
        select(AIRequest.status, AIRequest.model, func.count(), func.sum(AIRequest.total_tokens),
               func.sum(AIRequest.estimated_cost_usd))
        .where(AIRequest.scan_id == s.id, AIRequest.business_id == ctx.business_id)
        .group_by(AIRequest.status, AIRequest.model)
    )).all()
    methods = (await db.execute(
        select(ProductEvidence.extraction_method, ProductEvidence.source_type,
               func.count(func.distinct(ProductEvidence.product_id)))
        .where(ProductEvidence.scan_id == s.id, ProductEvidence.business_id == ctx.business_id)
        .group_by(ProductEvidence.extraction_method, ProductEvidence.source_type)
    )).all()
    ai = {"requests": 0, "succeeded": 0, "cached": 0, "failed": 0, "tokens": 0, "estimated_cost_usd": 0.0, "models": []}
    for status, model, n, tokens, cost in ai_rows:
        ai["requests"] += n
        ai[{"success": "succeeded", "cached": "cached"}.get(status, "failed")] += n
        ai["tokens"] += int(tokens or 0)
        ai["estimated_cost_usd"] = round(ai["estimated_cost_usd"] + float(cost or 0), 6)
        if model not in ai["models"]:
            ai["models"].append(model)
    return {
        **scan_dict(s, await db.get(Website, s.website_id)),
        "ai": ai,
        "extraction_methods": [{"method": m, "source_type": t, "products": n} for m, t, n in methods],
    }


@router.get("/{scan_id}/progress")
async def scan_progress(scan_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await get_owned_scan(db, ctx, scan_id)
    done = {h["stage"] for h in (s.stage_history or []) if h.get("finished_at")}
    website = await db.get(Website, s.website_id)
    return {
        "id": str(s.id), "status": s.status, "stage": s.stage, "outcome": scan_outcome(s),
        "website_url": website.normalized_url if website else None,
        "stages": [{"key": k, "state": "done" if k in done else ("active" if k == s.stage and s.status == "running" else
                                                              ("failed" if k == s.stage and s.status == "failed" else "pending"))}
                   for k in STAGES],
        "counters": {"pages_discovered": s.pages_discovered, "pages_crawled": s.pages_crawled,
                     "pages_failed": s.pages_failed, "product_pages_found": s.product_pages_found,
                     "products_extracted": s.products_extracted, "ai_calls": s.ai_calls},
        "errors": (s.errors or [])[-10:], "change_summary": s.change_summary or {},
    }


@router.post("/{scan_id}/cancel")
async def cancel_scan(scan_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await get_owned_scan(db, ctx, scan_id)
    if s.status not in ("queued", "running"):
        raise AppError(409, "not_cancellable", f"Scan is {s.status}")
    s.cancel_requested = True
    if s.status == "queued":
        s.status = "cancelled"
    await db.commit()
    return scan_dict(s)


@router.get("/{scan_id}/changes")
async def scan_changes(scan_id: str, change_type: str | None = None, ctx: AuthContext = Depends(current_auth),
                       db: AsyncSession = Depends(get_db)):
    s = await get_owned_scan(db, ctx, scan_id)
    q = select(ScanProductSnapshot).where(ScanProductSnapshot.scan_id == s.id, ScanProductSnapshot.business_id == ctx.business_id)
    if change_type:
        q = q.where(ScanProductSnapshot.change_type == change_type)
    snaps = (await db.execute(q)).scalars().all()
    names = {}
    ids = [sn.product_id for sn in snaps if sn.product_id]
    if ids:
        names = {p.id: p.name for p in (await db.execute(select(Product).where(Product.id.in_(ids)))).scalars()}
    return {"items": [{
        "id": str(sn.id), "change_type": sn.change_type, "decision": sn.decision,
        "product_id": str(sn.product_id) if sn.product_id else None,
        "name": (sn.data or {}).get("name") or names.get(sn.product_id),
        "match_key": sn.match_key, "changes": sn.changes,
        "price": (sn.data or {}).get("price"), "source_url": (sn.data or {}).get("source_url"),
    } for sn in snaps if sn.change_type != "unchanged" or change_type == "unchanged"],
        "summary": s.change_summary or {}}


@router.post("/{scan_id}/apply")
async def apply_changes(scan_id: str, body: ApplyScanIn, ctx: AuthContext = Depends(current_auth),
                        db: AsyncSession = Depends(get_db)):
    s = await get_owned_scan(db, ctx, scan_id)
    if s.status != "awaiting_review":
        raise AppError(409, "not_awaiting_review", f"Scan is {s.status}; nothing to apply")
    ids = None
    if body.snapshot_ids is not None:
        try:
            ids = {uuid.UUID(i) for i in body.snapshot_ids}
        except ValueError:
            raise AppError(422, "invalid_id", "Invalid snapshot id")
    counts = await apply_scan(db, s, ids)
    s.change_summary = {**(s.change_summary or {}), "applied": counts}
    from app.services.commerce.audit import actor_from_ctx, record

    record(db, ctx.business_id, actor_from_ctx(ctx), "scan.changes_applied", "website", s.website_id,
           f"Website changes applied: {counts.get('created', 0)} created, {counts.get('updated', 0)} updated, "
           f"{counts.get('removed', 0)} removed, {counts.get('skipped', 0)} skipped", {"scan_id": s.id, **counts})
    await db.commit()
    return {"scan": scan_dict(s), "applied": counts}


@router.get("/{scan_id}/pages")
async def scan_pages(scan_id: str, page_type: str | None = None, ctx: AuthContext = Depends(current_auth),
                     db: AsyncSession = Depends(get_db)):
    s = await get_owned_scan(db, ctx, scan_id)
    q = select(CrawledPage).where(CrawledPage.scan_id == s.id, CrawledPage.business_id == ctx.business_id)
    if page_type:
        q = q.where(CrawledPage.page_type == page_type)
    rows = (await db.execute(q.order_by(CrawledPage.fetched_at))).scalars().all()
    return {"items": [{
        "id": str(p.id), "url": p.url, "title": p.title, "status_code": p.status_code, "page_type": p.page_type,
        "product_score": p.product_score, "signals": p.signals, "error": p.error, "depth": p.depth,
        "render_mode": p.render_mode, "byte_size": p.byte_size,
    } for p in rows]}
