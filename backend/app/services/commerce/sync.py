"""Scheduled rescans, synchronization history and conflict resolution.

Conflicts: when a website value changes for a field the merchant manages in AgentReady
(``manual_overrides``), the change is never applied automatically. A SyncConflict records the
website value, the AgentReady value and the original (previous website) value, and waits for
the merchant unless the website's conflict policy says otherwise.
"""

from __future__ import annotations

import logging
import uuid
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.models import (
    ACTIVE_SCAN_STATUSES,
    Business,
    Product,
    ProductAttribute,
    ScanJob,
    ScanProductSnapshot,
    SyncConflict,
    SyncJob,
    SyncSchedule,
    Website,
)
from app.services.commerce.audit import Actor, jsonable, record
from app.services.commerce.money import aware, now_utc

log = logging.getLogger("agentready.sync")

FREQUENCIES = ("manual", "daily", "weekly", "monthly")
EXTRACTION_METHODS = ("structured_data", "tables", "html", "ai")
DETECT_KINDS = ("new_products", "removed_products", "price", "description", "images", "name", "sku", "availability",
                "specifications", "details")
CONFLICT_POLICIES = ("review", "keep_agentready", "accept_website")
DEFAULT_DETECT = {k: True for k in DETECT_KINDS}
# change-report category of each tracked field -> detection switch
FIELD_KIND = {
    "name": "name", "sku": "sku", "brand": "details", "manufacturer": "details", "category": "details",
    "description": "description", "price_amount": "price", "currency": "price", "availability": "availability",
    "gtin": "details", "mpn": "details", "material": "specifications", "dimensions": "specifications",
    "weight": "specifications", "images": "images", "attributes": "specifications", "product_url": "details",
}
OVERRIDE_NAME = {"price_amount": "price"}
SUGGEST_KEEP = {"price_amount", "currency", "availability", "category"}


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


# ---------------------------------------------------------------- schedules
def next_run(s: SyncSchedule, after: datetime) -> datetime | None:
    """Next run strictly after ``after`` in the schedule's timezone (DST-aware), returned in UTC."""
    if not s.enabled or s.frequency == "manual":
        return None
    tz = ZoneInfo(s.timezone or "UTC")
    local = aware(after).astimezone(tz)

    def at(d) -> datetime:
        return datetime(d.year, d.month, d.day, s.hour, s.minute, tzinfo=tz)

    if s.frequency == "daily":
        cand = at(local)
        if cand <= local:
            cand = at(local + timedelta(days=1))
    elif s.frequency == "weekly":
        cand = at(local + timedelta(days=(s.weekday - local.weekday()) % 7))
        if cand <= local:
            cand = at(cand + timedelta(days=7))
    else:  # monthly
        day = min(s.day_of_month, 28)
        cand = datetime(local.year, local.month, day, s.hour, s.minute, tzinfo=tz)
        if cand <= local:
            y, m = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
            cand = datetime(y, m, min(day, monthrange(y, m)[1]), s.hour, s.minute, tzinfo=tz)
    return cand.astimezone(timezone.utc)


def schedule_dict(s: SyncSchedule, website: Website | None = None) -> dict:
    return {
        "id": str(s.id), "website_id": str(s.website_id), "website_url": website.normalized_url if website else None,
        "frequency": s.frequency, "enabled": s.enabled, "timezone": s.timezone, "hour": s.hour, "minute": s.minute,
        "weekday": s.weekday, "day_of_month": s.day_of_month,
        "next_run_at": aware(s.next_run_at).isoformat() if s.next_run_at else None,
        "last_run_at": aware(s.last_run_at).isoformat() if s.last_run_at else None,
        "last_scan_id": str(s.last_scan_id) if s.last_scan_id else None, "last_error": s.last_error,
        "max_pages": s.max_pages if s.max_pages is not None else (website.max_pages if website else None),
        "max_products": s.max_products,
        "extraction_methods": s.extraction_methods or list(EXTRACTION_METHODS),
        "detect": {**DEFAULT_DETECT, **(s.detect or {})}, "conflict_policy": s.conflict_policy, "auto_apply": s.auto_apply,
        "updated_at": aware(s.updated_at).isoformat() if s.updated_at else None,
    }


async def get_schedule(db: AsyncSession, website: Website) -> SyncSchedule | None:
    return (await db.execute(select(SyncSchedule).where(SyncSchedule.website_id == website.id))).scalar_one_or_none()


def scan_settings_from(schedule: SyncSchedule | None) -> dict:
    if schedule is None:
        return {}
    out = {"methods": schedule.extraction_methods or list(EXTRACTION_METHODS),
           "detect": {**DEFAULT_DETECT, **(schedule.detect or {})},
           "conflict_policy": schedule.conflict_policy, "auto_apply": schedule.auto_apply}
    if schedule.max_pages:
        out["max_pages"] = schedule.max_pages
    if schedule.max_products:
        out["max_products"] = schedule.max_products
    return out


# ---------------------------------------------------------------- starting scans (manual + scheduled)
async def fail_stale_scans(db: AsyncSession, business_id: uuid.UUID) -> int:
    s = get_settings()
    cutoff = now_utc() - timedelta(minutes=s.scan_stale_after_minutes)
    stale = (await db.execute(select(ScanJob).where(
        ScanJob.business_id == business_id, ScanJob.status.in_(ACTIVE_SCAN_STATUSES), ScanJob.updated_at < cutoff
    ))).scalars().all()
    for scan in stale:
        scan.status = "failed"
        scan.finished_at = now_utc()
        scan.errors = [*(scan.errors or []), {
            "stage": scan.stage, "url": None, "at": now_utc().isoformat(), "level": "error",
            "message": "The scan stopped reporting progress (worker restarted or crashed).",
        }]
        await finalize_scan_sync(db, scan)
    return len(stale)


async def create_scan(db: AsyncSession, business: Business, website: Website, *, trigger: str, actor: Actor,
                      overrides: dict | None = None) -> tuple[ScanJob, SyncJob]:
    """Create a queued scan + its sync-history record. Raises AppError (409/429) when not allowed. Commits."""
    if await fail_stale_scans(db, business.id):
        await db.commit()
    active_here = (await db.execute(select(ScanJob.id).where(
        ScanJob.website_id == website.id, ScanJob.status.in_(ACTIVE_SCAN_STATUSES)))).first()
    if active_here:
        raise AppError(409, "scan_in_progress", "A scan is already running for this website")
    active = (await db.execute(select(func.count()).select_from(ScanJob).where(
        ScanJob.business_id == business.id, ScanJob.status.in_(ACTIVE_SCAN_STATUSES)))).scalar_one()
    limit = get_settings().max_active_scans_per_business
    if active >= limit:
        raise AppError(429, "scan_limit", f"You can run at most {limit} scans at the same time. Wait for one to finish.")
    has_products = (await db.execute(select(func.count()).select_from(Product).where(
        Product.website_id == website.id, Product.business_id == business.id))).scalar_one() > 0
    schedule = await get_schedule(db, website)
    settings = {**scan_settings_from(schedule), **{k: v for k, v in (overrides or {}).items() if v is not None},
                "trigger": trigger}
    scan = ScanJob(business_id=business.id, website_id=website.id, status="queued", stage="queued",
                   is_rescan=has_products, settings=settings, stage_history=[], errors=[], change_summary={})
    db.add(scan)
    try:
        await db.flush()
    except IntegrityError:  # concurrent request created the active scan first (unique partial index)
        await db.rollback()
        raise AppError(409, "scan_in_progress", "A scan is already running for this website")
    job = SyncJob(business_id=business.id, source_type="website", website_id=website.id, scan_id=scan.id,
                  source_label=website.normalized_url, direction="import", trigger=trigger, status="queued",
                  created_by=actor.id, errors=[], summary={})
    db.add(job)
    record(db, business.id, actor, "scan.started", "website", website.id,
           f"{'Scheduled' if trigger == 'scheduled' else 'Manual'} {'rescan' if has_products else 'scan'} of {website.normalized_url} started",
           {"scan_id": scan.id, "trigger": trigger})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError(409, "scan_in_progress", "A scan is already running for this website")
    return scan, job


async def dispatch_or_fail(db: AsyncSession, scan: ScanJob) -> str:
    from app.workers.dispatch import dispatch_scan

    try:
        return dispatch_scan(scan.id)
    except Exception as e:  # broker down: never leave a scan stuck in "queued"
        log.error("could not dispatch scan %s: %s", scan.id, type(e).__name__)
        await db.execute(update(ScanJob).where(ScanJob.id == scan.id).values(
            status="failed", errors=[{"stage": "queued", "url": None, "at": now_utc().isoformat(), "level": "error",
                                      "message": "The background worker is unavailable. Please try again shortly."}]))
        await db.execute(update(SyncJob).where(SyncJob.scan_id == scan.id).values(
            status="failed", finished_at=now_utc(), errors=[{"message": "Background worker unavailable"}]))
        await db.commit()
        raise AppError(503, "worker_unavailable", "The scan could not be started: the background worker is unavailable.")


async def dispatch_due_scans(db: AsyncSession, at: datetime | None = None) -> dict:
    """Start scans whose schedule is due. Each schedule is claimed with a conditional UPDATE on
    ``next_run_at`` so concurrent schedulers never start the same run twice."""
    at = at or now_utc()
    due = (await db.execute(select(SyncSchedule).where(
        SyncSchedule.enabled.is_(True), SyncSchedule.frequency != "manual", SyncSchedule.next_run_at.is_not(None),
        SyncSchedule.next_run_at <= at))).scalars().all()
    started = skipped = 0
    for s in due:
        claimed_slot = s.next_run_at
        new_next = next_run(s, at)
        res = await db.execute(update(SyncSchedule).where(
            SyncSchedule.id == s.id, SyncSchedule.next_run_at == claimed_slot
        ).values(next_run_at=new_next, last_run_at=at).execution_options(synchronize_session=False))
        await db.commit()
        if res.rowcount != 1:
            continue
        website = await db.get(Website, s.website_id)
        business = await db.get(Business, s.business_id)
        if website is None or business is None:
            continue
        try:
            scan, _ = await create_scan(db, business, website, trigger="scheduled", actor=Actor.system("Scheduler"))
            await dispatch_or_fail(db, scan)
            await db.execute(update(SyncSchedule).where(SyncSchedule.id == s.id).values(last_scan_id=scan.id, last_error=None))
            started += 1
        except AppError as e:
            await db.rollback()
            await db.execute(update(SyncSchedule).where(SyncSchedule.id == s.id).values(last_error=f"Skipped: {e.message}"))
            record(db, s.business_id, Actor.system("Scheduler"), "scan.schedule_skipped", "website", s.website_id,
                   f"Scheduled scan skipped: {e.message}")
            skipped += 1
        await db.commit()
    return {"started": started, "skipped": skipped}


# ---------------------------------------------------------------- sync history
def sync_job_dict(j: SyncJob) -> dict:
    return {
        "id": str(j.id), "source_type": j.source_type, "source_label": j.source_label,
        "website_id": str(j.website_id) if j.website_id else None,
        "integration_id": str(j.integration_id) if j.integration_id else None,
        "scan_id": str(j.scan_id) if j.scan_id else None, "direction": j.direction, "trigger": j.trigger,
        "status": j.status, "products_discovered": j.products_discovered, "products_created": j.products_created,
        "products_changed": j.products_changed, "products_ignored": j.products_ignored,
        "conflicts_detected": j.conflicts_detected, "failed_operations": j.failed_operations,
        "errors": (j.errors or [])[:50], "summary": j.summary or {},
        "created_at": aware(j.created_at).isoformat() if j.created_at else None,
        "started_at": aware(j.started_at).isoformat() if j.started_at else None,
        "finished_at": aware(j.finished_at).isoformat() if j.finished_at else None,
    }


async def finalize_scan_sync(db: AsyncSession, scan: ScanJob) -> SyncJob | None:
    job = (await db.execute(select(SyncJob).where(SyncJob.scan_id == scan.id))).scalar_one_or_none()
    if job is None:
        return None
    summary = scan.change_summary or {}
    applied = summary.get("applied") or {}
    job.started_at = scan.started_at or job.started_at
    job.products_discovered = scan.products_extracted
    job.products_created = applied.get("created", 0) if applied else 0
    job.products_changed = applied.get("updated", 0) if applied else 0
    job.products_ignored = int(summary.get("ignored_changes", 0)) + int(summary.get("ignored_deleted", 0)) + int(applied.get("skipped", 0))
    job.conflicts_detected = int(summary.get("conflicts", 0))
    job.failed_operations = scan.pages_failed + sum(1 for e in scan.errors or [] if (e.get("level") or "error") == "error")
    job.errors = [{"message": e.get("message"), "url": e.get("url"), "level": e.get("level")}
                  for e in (scan.errors or []) if (e.get("level") or "error") != "info"][:50]
    job.summary = {k: v for k, v in summary.items() if k != "applied"} | ({"applied": applied} if applied else {})
    if scan.status == "awaiting_review":
        job.status = "awaiting_review"
    elif scan.status == "completed":
        job.status = "completed_with_errors" if job.failed_operations else "completed"
        job.finished_at = scan.applied_at or scan.finished_at or now_utc()
    elif scan.status in ("failed", "cancelled"):
        job.status = scan.status
        job.finished_at = scan.finished_at or now_utc()
    else:
        job.status = "running" if scan.status == "running" else job.status
    return job


# ---------------------------------------------------------------- conflicts
def _suggest(field: str, product: Product) -> tuple[str, str]:
    if field == "availability" and product.track_inventory:
        return "keep_agentready", "Stock is tracked in AgentReady, so its availability is computed from your inventory."
    if field in SUGGEST_KEEP:
        return "keep_agentready", ("You changed this in AgentReady. Keep your value unless the website change was "
                                   "intentional.")
    return "accept_website", "Content such as descriptions and images is usually maintained on the website."


async def record_conflicts(db: AsyncSession, scan: ScanJob, product: Product, changes: list[dict], data: dict) -> int:
    """Create (or refresh) an open conflict for each change that touches a merchant-managed field."""
    from app.services.catalog.service import product_fields

    job_id = (await db.execute(select(SyncJob.id).where(SyncJob.scan_id == scan.id))).scalar_one_or_none()
    current = product_fields(product)
    n = 0
    for c in changes:
        if not c.get("manual_override"):
            continue
        f = c["field"]
        evidence = [jsonable(e) for e in (data.get("evidence") or []) if e.get("field", "").split(".")[0] in
                    ({"price"} if f == "price_amount" else {f})][:5]
        original = (product.source_values or {}).get(f)
        action, reason = _suggest(f, product)
        existing = (await db.execute(select(SyncConflict).where(
            SyncConflict.product_id == product.id, SyncConflict.field == f, SyncConflict.status == "open"))).scalar_one_or_none()
        if existing is not None:
            existing.website_value, existing.agentready_value = jsonable(c["new"]), jsonable(current.get(f))
            existing.scan_id, existing.sync_job_id, existing.evidence = scan.id, job_id, evidence
            existing.detected_at = now_utc()
        else:
            db.add(SyncConflict(
                business_id=product.business_id, product_id=product.id, sync_job_id=job_id, scan_id=scan.id,
                source_type="website", field=f, website_value=jsonable(c["new"]), agentready_value=jsonable(current.get(f)),
                original_value=jsonable(original), evidence=evidence, suggested_action=action, suggestion_reason=reason,
            ))
            record(db, product.business_id, Actor.system("Website sync"), "sync.conflict_detected", "product", product.id,
                   f"Conflict on '{product.name}': website changed {f} but you manage it in AgentReady",
                   {"field": f, "website_value": c["new"], "agentready_value": current.get(f)})
        n += 1
    return n


def conflict_dict(c: SyncConflict, product_name: str | None = None) -> dict:
    return {
        "id": str(c.id), "product_id": str(c.product_id), "product_name": product_name, "field": c.field,
        "website_value": c.website_value, "agentready_value": c.agentready_value, "original_value": c.original_value,
        "evidence": c.evidence or [], "suggested_action": c.suggested_action, "suggestion_reason": c.suggestion_reason,
        "status": c.status, "resolution": c.resolution, "resolved_value": c.resolved_value,
        "sync_job_id": str(c.sync_job_id) if c.sync_job_id else None, "scan_id": str(c.scan_id) if c.scan_id else None,
        "source_type": c.source_type, "detected_at": aware(c.detected_at).isoformat() if c.detected_at else None,
        "resolved_at": aware(c.resolved_at).isoformat() if c.resolved_at else None,
    }


RESOLUTIONS = ("accept_website", "keep_agentready", "edit", "ignore")
TEXT_LIMITS = {"name": 500, "sku": 200, "brand": 200, "manufacturer": 200, "category": 300, "description": 20000,
               "mpn": 100, "material": 200, "product_url": 2048, "gtin": 14}


async def _write_field(db: AsyncSession, p: Product, field: str, value, actor: Actor, scan_id: uuid.UUID | None,
                       from_website: bool) -> None:
    from app.schemas.canonical import AVAILABILITY_VALUES
    from app.services.commerce.pricing import set_pricing

    business = await db.get(Business, p.business_id)
    if field == "price_amount":
        await set_pricing(db, p, actor=actor, change_type="conflict_resolution", regular=value,
                          default_currency=business.default_currency if business else None, mark_override=not from_website)
    elif field == "currency":
        if value is not None and (not isinstance(value, str) or len(value) != 3):
            raise AppError(422, "invalid_value", "Currency must be a 3-letter ISO code")
        await set_pricing(db, p, actor=actor, change_type="conflict_resolution", currency=value,
                          mark_override=not from_website)
    elif field == "availability":
        if value is not None and value not in AVAILABILITY_VALUES:
            raise AppError(422, "invalid_value", f"'{value}' is not a valid availability")
        p.availability = value
    elif field in ("images", "attributes"):
        snap = None
        if scan_id:
            snap = (await db.execute(select(ScanProductSnapshot).where(
                ScanProductSnapshot.scan_id == scan_id, ScanProductSnapshot.product_id == p.id))).scalar_one_or_none()
        data = (snap.data or {}) if snap else {}
        if not from_website:
            raise AppError(422, "invalid_value", f"Edit {field} on the product page instead")
        if field == "images":
            p.images = data.get("images") or p.images
        else:
            merchant = [a for a in p.attributes if a.source == "merchant"]
            p.attributes = merchant + [
                ProductAttribute(key=a["key"], label=a["label"], value=a.get("value"), unit=a.get("unit"),
                                 raw_value=a.get("raw_value"), source="extracted", position=len(merchant) + i)
                for i, a in enumerate(data.get("attributes") or [])]
    elif field in ("dimensions", "weight"):
        if not from_website:
            raise AppError(422, "invalid_value", f"Edit {field} on the product page instead")
        setattr(p, field, value)
    elif field in TEXT_LIMITS:
        if value is not None and (not isinstance(value, str) or len(value) > TEXT_LIMITS[field]):
            raise AppError(422, "invalid_value", f"{field} must be text of at most {TEXT_LIMITS[field]} characters")
        setattr(p, field, value)
    else:
        raise AppError(422, "invalid_field", f"Field {field} cannot be resolved here")


async def resolve_conflict(db: AsyncSession, c: SyncConflict, action: str, actor: Actor, value=None) -> SyncConflict:
    if c.status != "open":
        raise AppError(409, "already_resolved", "This conflict has already been resolved")
    if action not in RESOLUTIONS:
        raise AppError(422, "invalid_action", f"Action must be one of {', '.join(RESOLUTIONS)}")
    p = await db.get(Product, c.product_id)
    if p is None:
        raise AppError(404, "not_found", "Product not found")
    overrides = set(p.manual_overrides or [])
    override_name = OVERRIDE_NAME.get(c.field, c.field)
    resolved_value = None
    if action == "accept_website":
        await _write_field(db, p, c.field, c.website_value, actor, c.scan_id, from_website=True)
        overrides.discard(override_name)  # the product follows the website again for this field
        p.manual_overrides = sorted(overrides)
        resolved_value = c.website_value
    elif action == "edit":
        if value is None:
            raise AppError(422, "value_required", "Provide the value to use")
        await _write_field(db, p, c.field, value, actor, c.scan_id, from_website=False)
        p.manual_overrides = sorted(overrides | {override_name})
        resolved_value = value
    elif action == "keep_agentready":
        resolved_value = c.agentready_value
    if action != "ignore":
        sv = dict(p.source_values or {})
        sv[c.field] = c.website_value  # acknowledged: the same website value will not raise a conflict again
        p.source_values = sv
    if action in ("accept_website", "edit"):
        p.version += 1
        p.updated_at = now_utc()
    c.status, c.resolution = "resolved", {"edit": "edited", "ignore": "ignored"}.get(action, action)
    c.resolved_value, c.resolved_by, c.resolved_at = jsonable(resolved_value), actor.id, now_utc()
    record(db, p.business_id, actor, "sync.conflict_resolved", "product", p.id,
           f"Conflict on '{p.name}' ({c.field}) resolved: {c.resolution.replace('_', ' ')}",
           {"field": c.field, "resolution": c.resolution, "website_value": c.website_value,
            "agentready_value": c.agentready_value, "resolved_value": resolved_value})
    return c


async def apply_conflict_policy(db: AsyncSession, scan: ScanJob) -> int:
    policy = (scan.settings or {}).get("conflict_policy", "review")
    if policy == "review":
        return 0
    open_conflicts = (await db.execute(select(SyncConflict).where(
        SyncConflict.scan_id == scan.id, SyncConflict.status == "open"))).scalars().all()
    action = "keep_agentready" if policy == "keep_agentready" else "accept_website"
    for c in open_conflicts:
        await resolve_conflict(db, c, action, Actor.system(f"Conflict policy: {policy.replace('_', ' ')}"))
    return len(open_conflicts)
