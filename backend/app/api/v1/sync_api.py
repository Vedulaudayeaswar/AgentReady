"""Synchronization: schedules/settings per website, sync history, conflicts."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.websites import get_owned_website
from app.core.errors import AppError, not_found
from app.core.ratelimit import rate_limit
from app.db.session import get_db
from app.models import CommerceIntegration, Product, SyncConflict, SyncJob, SyncSchedule, Website
from app.schemas.commerce import ResolveIn, ScheduleSettingsIn, SyncStartIn
from app.services.commerce.audit import actor_from_ctx, record
from app.services.commerce.money import now_utc
from app.services.commerce.sync import (
    DETECT_KINDS,
    EXTRACTION_METHODS,
    conflict_dict,
    create_scan,
    dispatch_or_fail,
    next_run,
    resolve_conflict,
    schedule_dict,
    sync_job_dict,
    valid_timezone,
)
from app.services.validation.validator import revalidate_business

router = APIRouter(prefix="/sync", tags=["synchronization"])


def _uuid(v, what):
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        raise not_found(what)


# ---------------------------------------------------------------- schedules
@router.get("/schedules")
async def list_schedules(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    sites = (await db.execute(select(Website).where(Website.business_id == ctx.business_id).order_by(Website.created_at))).scalars().all()
    schedules = {s.website_id: s for s in (await db.execute(select(SyncSchedule).where(
        SyncSchedule.business_id == ctx.business_id))).scalars()}
    items = []
    for w in sites:
        s = schedules.get(w.id)
        items.append(schedule_dict(s, w) if s else {
            "id": None, "website_id": str(w.id), "website_url": w.normalized_url, "frequency": "manual", "enabled": False,
            "timezone": "UTC", "hour": 3, "minute": 0, "weekday": 0, "day_of_month": 1, "next_run_at": None,
            "last_run_at": None, "last_scan_id": None, "last_error": None, "max_pages": w.max_pages, "max_products": None,
            "extraction_methods": list(EXTRACTION_METHODS), "detect": {k: True for k in DETECT_KINDS},
            "conflict_policy": "review", "auto_apply": False, "updated_at": None})
    return {"items": items, "extraction_methods": list(EXTRACTION_METHODS), "detect_kinds": list(DETECT_KINDS)}


async def _apply_settings(db: AsyncSession, s: SyncSchedule, body: ScheduleSettingsIn) -> dict:
    fields = body.model_fields_set - {"website_id"}
    if body.timezone is not None and not valid_timezone(body.timezone):
        raise AppError(422, "invalid_timezone", f"Unknown timezone '{body.timezone}' (use an IANA name such as Asia/Kolkata)")
    if body.detect is not None:
        unknown = set(body.detect) - set(DETECT_KINDS)
        if unknown:
            raise AppError(422, "invalid_detect", f"Unknown change kinds: {', '.join(sorted(unknown))}")
    changes = {}
    for f in fields:
        v = getattr(body, f)
        if v is None and f not in ("max_pages", "max_products"):
            continue
        if f == "detect":
            v = {**(s.detect or {}), **v}
        if getattr(s, f) != v:
            changes[f] = {"from": getattr(s, f), "to": v}
            setattr(s, f, v)
    if s.frequency == "manual":
        s.enabled = False if "enabled" not in fields or s.frequency == "manual" else s.enabled
    s.next_run_at = next_run(s, now_utc())
    return changes


@router.post("/schedules", status_code=201)
async def upsert_schedule(body: ScheduleSettingsIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    if not body.website_id:
        raise AppError(422, "website_required", "website_id is required")
    w = await get_owned_website(db, ctx, body.website_id)
    s = (await db.execute(select(SyncSchedule).where(SyncSchedule.website_id == w.id))).scalar_one_or_none()
    if s is None:
        s = SyncSchedule(business_id=ctx.business_id, website_id=w.id, frequency="manual", enabled=False, timezone="UTC",
                         hour=3, minute=0, weekday=0, day_of_month=1, extraction_methods=list(EXTRACTION_METHODS),
                         detect={k: True for k in DETECT_KINDS}, conflict_policy="review", auto_apply=False)
        db.add(s)
    changes = await _apply_settings(db, s, body)
    await db.flush()
    record(db, ctx.business_id, actor_from_ctx(ctx), "sync.schedule_updated", "website", w.id,
           f"Sync settings for {w.normalized_url}: {s.frequency}{' (enabled)' if s.enabled else ''}", changes)
    await db.commit()
    return schedule_dict(s, w)


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(schedule_id: str, body: ScheduleSettingsIn, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(SyncSchedule).where(SyncSchedule.id == _uuid(schedule_id, "Schedule"),
                                                     SyncSchedule.business_id == ctx.business_id))).scalar_one_or_none()
    if s is None:
        raise not_found("Schedule")
    w = await db.get(Website, s.website_id)
    changes = await _apply_settings(db, s, body)
    record(db, ctx.business_id, actor_from_ctx(ctx), "sync.schedule_updated", "website", s.website_id,
           f"Sync settings for {w.normalized_url if w else 'website'} updated", changes)
    await db.commit()
    return schedule_dict(s, w)


# ---------------------------------------------------------------- history + start
@router.get("")
async def list_syncs(source_type: str | None = None, status: str | None = None, website_id: str | None = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    q = select(SyncJob).where(SyncJob.business_id == ctx.business_id)
    if source_type:
        q = q.where(SyncJob.source_type.in_(source_type.split(",")))
    if status:
        q = q.where(SyncJob.status.in_(status.split(",")))
    if website_id:
        q = q.where(SyncJob.website_id == _uuid(website_id, "Website"))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(SyncJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    open_conflicts = (await db.execute(select(func.count()).select_from(SyncConflict).where(
        SyncConflict.business_id == ctx.business_id, SyncConflict.status == "open"))).scalar_one()
    return {"items": [sync_job_dict(j) for j in rows], "total": total, "page": page, "page_size": page_size,
            "open_conflicts": open_conflicts}


@router.post("", status_code=202, dependencies=[Depends(rate_limit("start_sync", limit=10))])
async def start_sync(body: SyncStartIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Start a website rescan (website_id) or an integration import/export (integration_id)."""
    if bool(body.website_id) == bool(body.integration_id):
        raise AppError(422, "source_required", "Provide exactly one of website_id or integration_id")
    if body.website_id:
        w = await get_owned_website(db, ctx, body.website_id)
        scan, job = await create_scan(db, ctx.business, w, trigger="manual", actor=actor_from_ctx(ctx))
        backend = await dispatch_or_fail(db, scan)
        return {**sync_job_dict(job), "job_backend": backend}
    from app.api.v1.integrations import start_integration_sync

    i = (await db.execute(select(CommerceIntegration).where(
        CommerceIntegration.id == _uuid(body.integration_id, "Integration"),
        CommerceIntegration.business_id == ctx.business_id))).scalar_one_or_none()
    if i is None:
        raise not_found("Integration")
    return await start_integration_sync(db, ctx, i, body.direction)


@router.get("/conflicts")
async def list_conflicts(status: str = "open", product_id: str | None = None, page: int = Query(1, ge=1),
                         page_size: int = Query(50, ge=1, le=200), ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    q = select(SyncConflict, Product.name).join(Product, Product.id == SyncConflict.product_id).where(
        SyncConflict.business_id == ctx.business_id)
    if status != "all":
        q = q.where(SyncConflict.status == status)
    if product_id:
        q = q.where(SyncConflict.product_id == _uuid(product_id, "Product"))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(SyncConflict.detected_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [conflict_dict(c, n) for c, n in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_one(conflict_id: str, body: ResolveIn, ctx: AuthContext = Depends(current_auth),
                      db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(SyncConflict).where(SyncConflict.id == _uuid(conflict_id, "Conflict"),
                                                     SyncConflict.business_id == ctx.business_id))).scalar_one_or_none()
    if c is None:
        raise not_found("Conflict")
    await resolve_conflict(db, c, body.action, actor_from_ctx(ctx), body.value)
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    p = await db.get(Product, c.product_id)
    return conflict_dict(c, p.name if p else None)


@router.get("/{sync_id}")
async def get_sync(sync_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    j = (await db.execute(select(SyncJob).where(SyncJob.id == _uuid(sync_id, "Sync"),
                                                SyncJob.business_id == ctx.business_id))).scalar_one_or_none()
    if j is None:
        raise not_found("Sync")
    rows = (await db.execute(select(SyncConflict, Product.name).join(Product, Product.id == SyncConflict.product_id)
                             .where(SyncConflict.sync_job_id == j.id, SyncConflict.business_id == ctx.business_id)
                             .order_by(SyncConflict.status, SyncConflict.detected_at.desc()))).all()
    return {**sync_job_dict(j), "conflicts": [conflict_dict(c, n) for c, n in rows]}


@router.post("/{sync_id}/resolve")
async def resolve_for_sync(sync_id: str, body: ResolveIn, ctx: AuthContext = Depends(current_auth),
                           db: AsyncSession = Depends(get_db)):
    j = (await db.execute(select(SyncJob).where(SyncJob.id == _uuid(sync_id, "Sync"),
                                                SyncJob.business_id == ctx.business_id))).scalar_one_or_none()
    if j is None:
        raise not_found("Sync")
    q = select(SyncConflict).where(SyncConflict.sync_job_id == j.id, SyncConflict.business_id == ctx.business_id,
                                   SyncConflict.status == "open")
    if body.conflict_ids is not None:
        q = q.where(SyncConflict.id.in_([_uuid(x, "Conflict") for x in body.conflict_ids]))
    conflicts = (await db.execute(q)).scalars().all()
    if body.action == "edit" and len(conflicts) != 1:
        raise AppError(422, "edit_one", "Edit conflicts one at a time")
    actor = actor_from_ctx(ctx)
    for c in conflicts:
        await resolve_conflict(db, c, body.action, actor, body.value)
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    return {"resolved": len(conflicts), "action": body.action}
