"""Periodic work: due scheduled rescans and due scheduled price changes.

* JOB_BACKEND=celery: Celery Beat runs ``agentready.scheduler_tick`` every minute (see celery_app).
* JOB_BACKEND=inline: the API process runs the same tick in an asyncio loop (local development).

Each tick is safe to run concurrently with another tick: schedules and price changes are claimed
with conditional UPDATEs before any work starts, so nothing runs twice.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.db.session import session_factory

log = logging.getLogger("agentready.scheduler")


async def _stage(out: dict, name: str, fn) -> None:
    """Run one scheduler stage in its own session. A failing stage is logged and reported in the tick
    result; it never prevents the other stages from running."""
    try:
        async with session_factory()() as db:
            out[name] = await fn(db)
    except Exception as e:  # noqa: BLE001
        log.exception("scheduler stage %s failed", name)
        out[name] = {"error": type(e).__name__}


async def tick() -> dict:
    from app.core.health import record_scheduler_heartbeat
    from app.services.commerce.pricing import apply_due_price_changes
    from app.services.commerce.sync import dispatch_due_scans
    from app.services.distribution.feeds import generate_due_feeds
    from app.services.intelligence.notifications import tick as intelligence_tick

    await record_scheduler_heartbeat()
    out: dict = {}
    await _stage(out, "scans", dispatch_due_scans)
    await _stage(out, "prices", apply_due_price_changes)
    await _stage(out, "feeds", generate_due_feeds)
    await _stage(out, "housekeeping", housekeeping)
    await _stage(out, "intelligence", intelligence_tick)  # per merchant at most every 10 minutes
    if any(v for d in out.values() for v in d.values()):
        log.info("scheduler tick: %s", out)
    return out


async def housekeeping(db) -> dict:
    """Drop dispatched outbox rows after 7 days and requeue webhook deliveries orphaned by a dead worker."""
    from datetime import timedelta

    from sqlalchemy import delete

    from app.models import OutboxEvent
    from app.services.commerce.money import now_utc
    from app.services.distribution.webhooks import recover_stuck

    from app.workers.recovery import recover_stale_jobs

    res = await db.execute(delete(OutboxEvent).where(OutboxEvent.dispatched_at.is_not(None),
                                                     OutboxEvent.dispatched_at < now_utc() - timedelta(days=7)))
    await db.commit()
    out = {"outbox_pruned": res.rowcount or 0, "webhooks_requeued": await recover_stuck(db)}
    out.update(await recover_stale_jobs(db))
    return out


async def webhook_tick() -> dict:
    """Fan out new product events and deliver due webhooks (runs every few seconds)."""
    from app.services.distribution.webhooks import tick as wh_tick

    async with session_factory()() as db:
        out = await wh_tick(db)
    if out.get("deliveries") or out.get("sent") or out.get("failed_attempts"):
        log.info("webhook tick: %s", out)
    return out


WEBHOOK_INTERVAL_SECONDS = 5


def start_inline_scheduler() -> list[asyncio.Task]:
    interval = max(10, get_settings().scheduler_interval_seconds)

    async def loop():
        while True:
            try:
                await tick()
            except Exception:  # keep the loop alive; errors are logged
                log.exception("scheduler tick failed")
            await asyncio.sleep(interval)

    async def hooks_loop():
        while True:
            try:
                await webhook_tick()
            except Exception:
                log.exception("webhook tick failed")
            await asyncio.sleep(WEBHOOK_INTERVAL_SECONDS)

    log.info("in-process scheduler started (every %ss; webhooks every %ss)", interval, WEBHOOK_INTERVAL_SECONDS)
    loop_ = asyncio.get_running_loop()
    return [loop_.create_task(loop()), loop_.create_task(hooks_loop())]
