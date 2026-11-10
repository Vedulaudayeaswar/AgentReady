"""System status for the signed-in merchant: real dependency health plus this merchant's background jobs.

Every count comes from the job tables (scans, sync jobs, feed snapshots, bulk operations, webhook
deliveries, reports). Failed jobs carry their recorded error and where in the app to retry them.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.db.session import get_db
from app.models import BulkOperation, FeedConfiguration, FeedSnapshot, ScanJob, SyncJob
from app.models.distribution import WebhookDelivery, WebhookSubscription
from app.models.intelligence import IntelligenceReport
from app.services.commerce.money import now_utc

router = APIRouter(prefix="/system", tags=["system"])

ACTIVE = ("queued", "running", "delivering", "pending")
FAILED = ("failed", "completed_with_errors")


def _iso(dt):
    return dt.isoformat() if dt else None


def _last_error(errors) -> str | None:
    for e in reversed(errors or []):
        if isinstance(e, dict) and e.get("level", "error") == "error" and e.get("message"):
            return str(e["message"])[:300]
    return None


@router.get("/status")
async def system_status(days: int = 7, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    from app.core.health import run_checks

    days = max(1, min(days, 90))
    bid = ctx.business_id
    since = now_utc() - timedelta(days=days)
    _, health = await run_checks()

    async def counts(model, status_col, created_col, extra=()):
        rows = (await db.execute(select(status_col, func.count()).where(
            model.business_id == bid, created_col >= since, *extra).group_by(status_col))).all()
        return {k: v for k, v in rows}

    jobs = {
        "scans": await counts(ScanJob, ScanJob.status, ScanJob.created_at),
        "syncs": await counts(SyncJob, SyncJob.status, SyncJob.created_at, (SyncJob.scan_id.is_(None),)),
        "feed_generation": await counts(FeedSnapshot, FeedSnapshot.status, FeedSnapshot.created_at),
        "bulk_operations": await counts(BulkOperation, BulkOperation.status, BulkOperation.created_at),
        "webhook_deliveries": await counts(WebhookDelivery, WebhookDelivery.status, WebhookDelivery.created_at),
        "reports": await counts(IntelligenceReport, IntelligenceReport.status, IntelligenceReport.created_at),
    }

    failures: list[dict] = []
    for sj in (await db.execute(select(ScanJob).where(ScanJob.business_id == bid, ScanJob.created_at >= since,
                                                      ScanJob.status == "failed")
                                .order_by(ScanJob.created_at.desc()).limit(10))).scalars():
        failures.append({"kind": "scan", "id": str(sj.id), "status": sj.status, "at": _iso(sj.finished_at or sj.created_at),
                         "error": _last_error(sj.errors) or "Scan failed", "retry": "Start a new scan from Scan history",
                         "link": f"/app/scans/{sj.id}"})
    for job in (await db.execute(select(SyncJob).where(SyncJob.business_id == bid, SyncJob.created_at >= since,
                                                       SyncJob.scan_id.is_(None), SyncJob.status.in_(FAILED))
                                 .order_by(SyncJob.created_at.desc()).limit(10))).scalars():
        failures.append({"kind": "integration_sync", "id": str(job.id), "status": job.status,
                         "at": _iso(job.finished_at or job.created_at), "error": _last_error(job.errors) or job.status,
                         "retry": "Run the import again from Integrations", "link": "/app/integrations"})
    for snap, name in (await db.execute(select(FeedSnapshot, FeedConfiguration.name)
                                        .join(FeedConfiguration, FeedConfiguration.id == FeedSnapshot.feed_id)
                                        .where(FeedSnapshot.business_id == bid, FeedSnapshot.created_at >= since,
                                               FeedSnapshot.status == "failed")
                                        .order_by(FeedSnapshot.created_at.desc()).limit(10))).all():
        failures.append({"kind": "feed_generation", "id": str(snap.id), "status": snap.status, "name": name,
                         "at": _iso(snap.finished_at or snap.created_at), "error": (snap.error or "Generation failed")[:300],
                         "retry": "Select 'Generate now' on the feed", "link": "/app/distribution/feeds"})
    for op in (await db.execute(select(BulkOperation).where(BulkOperation.business_id == bid,
                                                            BulkOperation.created_at >= since,
                                                            BulkOperation.status.in_(FAILED))
                                .order_by(BulkOperation.created_at.desc()).limit(10))).scalars():
        failures.append({"kind": "bulk_operation", "id": str(op.id), "status": op.status, "name": op.operation,
                         "at": _iso(op.finished_at or op.created_at),
                         "error": (op.error or f"{op.failed} of {op.total} item(s) failed")[:300],
                         "retry": "Review the failed items and run the operation again for them", "link": "/app/catalog"})
    for d, url in (await db.execute(select(WebhookDelivery, WebhookSubscription.url)
                                    .join(WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id)
                                    .where(WebhookDelivery.business_id == bid, WebhookDelivery.created_at >= since,
                                           WebhookDelivery.status == "failed")
                                    .order_by(WebhookDelivery.created_at.desc()).limit(10))).all():
        failures.append({"kind": "webhook_delivery", "id": str(d.id), "status": d.status, "name": d.event_type,
                         "at": _iso(d.created_at), "error": (d.last_error or f"HTTP {d.last_status_code}")[:300],
                         "detail": f"{d.attempts} attempt(s) to {url.split('?')[0]}",
                         "retry": "Use 'Redeliver' on the Webhooks page", "link": "/app/distribution/webhooks"})
    failures.sort(key=lambda f: f["at"] or "", reverse=True)

    active = sum(v for group in jobs.values() for k, v in group.items() if k in ACTIVE)
    return {"health": health, "period_days": days, "jobs": jobs, "active_jobs": active, "recent_failures": failures[:30]}
