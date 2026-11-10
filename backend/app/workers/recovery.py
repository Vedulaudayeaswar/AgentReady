"""Recovery of background jobs interrupted by a worker crash or restart (run by the scheduler tick).

A job is never reported as completed when it did not finish: interrupted work is either resumed (when
the job is idempotent) or marked ``failed`` with a merchant-readable reason, so the UI never shows a job
"running" forever and the merchant can retry it.

* Scans: no progress for ``SCAN_STALE_AFTER_MINUTES`` -> failed (existing rule, now applied to all
  merchants, not only when the merchant starts another scan).
* Feed snapshots: queued/running for ``JOB_STALE_AFTER_MINUTES`` -> failed ("Generate now" retries).
* Bulk operations: only pending items are ever processed, so an interrupted operation is resumed once;
  if it is interrupted again the remaining items are marked failed and the operation fails.
* Integration syncs (not linked to a scan): queued/running too long -> failed.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ACTIVE_SCAN_STATUSES, BulkOperation, BulkOperationItem, FeedSnapshot, ScanJob, SyncJob
from app.services.commerce.money import now_utc

log = logging.getLogger("agentready.recovery")
INTERRUPTED = "Interrupted: the background worker stopped before this job finished (restart or crash)."


async def recover_stale_jobs(db: AsyncSession) -> dict:
    from app.services.commerce.sync import fail_stale_scans

    s = get_settings()
    now = now_utc()
    cutoff = now - timedelta(minutes=s.job_stale_after_minutes)
    out = {"scans_failed": 0, "feed_snapshots_failed": 0, "bulk_resumed": 0, "bulk_failed": 0, "syncs_failed": 0}

    scan_cutoff = now - timedelta(minutes=s.scan_stale_after_minutes)
    businesses = (await db.execute(select(ScanJob.business_id).where(
        ScanJob.status.in_(ACTIVE_SCAN_STATUSES), ScanJob.updated_at < scan_cutoff).distinct())).scalars().all()
    for bid in businesses:
        out["scans_failed"] += await fail_stale_scans(db, bid)
    await db.commit()

    res = await db.execute(update(FeedSnapshot).where(
        FeedSnapshot.status.in_(("queued", "running")),
        or_(FeedSnapshot.started_at < cutoff, (FeedSnapshot.started_at.is_(None)) & (FeedSnapshot.created_at < cutoff)),
    ).values(status="failed", finished_at=now, error=INTERRUPTED + " Select 'Generate now' to build it again.")
        .execution_options(synchronize_session=False))
    out["feed_snapshots_failed"] = res.rowcount or 0
    await db.commit()

    stale_ops = (await db.execute(select(BulkOperation).where(
        BulkOperation.status.in_(("queued", "running")), BulkOperation.run_mode == "background",
        BulkOperation.updated_at < cutoff))).scalars().all()
    for op in stale_ops:
        result = dict(op.result or {})
        if int(result.get("recovery_attempts", 0)) < 1:
            result["recovery_attempts"] = int(result.get("recovery_attempts", 0)) + 1
            op.result = result
            op.updated_at = now
            await db.commit()
            from app.services.commerce.bulk import dispatch

            try:
                dispatch(op.id)
                out["bulk_resumed"] += 1
                log.warning("resumed interrupted bulk operation %s", op.id)
                continue
            except Exception:  # noqa: BLE001 - fall through to failing it
                log.exception("could not resume bulk operation %s", op.id)
        pending = await db.execute(update(BulkOperationItem).where(
            BulkOperationItem.operation_id == op.id, BulkOperationItem.status == "pending",
        ).values(status="failed", error=INTERRUPTED, processed_at=now).execution_options(synchronize_session=False))
        n = pending.rowcount or 0
        op.failed += n
        op.processed += n
        op.status = "failed" if op.succeeded == 0 else "completed_with_errors"
        op.error = f"{INTERRUPTED} {n} item(s) were not processed; changes already applied are kept."
        op.finished_at = now
        out["bulk_failed"] += 1
        await db.commit()

    stale_syncs = (await db.execute(select(SyncJob).where(
        SyncJob.status.in_(("queued", "running")), SyncJob.scan_id.is_(None), SyncJob.updated_at < cutoff,
    ))).scalars().all()
    for job in stale_syncs:
        job.status = "failed"
        job.finished_at = now
        job.errors = [*(job.errors or []), {"message": INTERRUPTED, "at": now.isoformat()}]
        out["syncs_failed"] += 1
    await db.commit()
    if any(out.values()):
        log.warning("recovered interrupted jobs: %s", out)
    return out
