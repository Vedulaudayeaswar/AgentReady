"""Celery application. Start a worker with:

    celery -A app.workers.celery_app worker --loglevel=INFO --concurrency=2
"""

from __future__ import annotations

import asyncio
import uuid

from celery import Celery

from app.core.config import get_settings

settings = get_settings()
celery_app = Celery("agentready", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=60 * 60,
    task_soft_time_limit=55 * 60,
    task_serializer="json",
    accept_content=["json"],
    # A task whose worker process is killed (OOM, restart) is redelivered instead of silently lost.
    task_reject_on_worker_lost=True,
    # Redis redelivers unacknowledged tasks after the visibility timeout: keep it above the task time
    # limit, otherwise a long scan would be started a second time while still running.
    broker_transport_options={"visibility_timeout": 2 * 60 * 60},
    broker_connection_retry_on_startup=True,
    # Recycle worker processes periodically (bounds memory growth from large crawls).
    worker_max_tasks_per_child=200,
    worker_send_task_events=False,
    result_expires=24 * 60 * 60,
    # Celery Beat: `celery -A app.workers.celery_app beat` (one beat process per deployment)
    beat_schedule={"agentready-scheduler-tick": {"task": "agentready.scheduler_tick", "schedule": 60.0},
                   # distribution webhooks: fan out the outbox and deliver due webhooks
                   "agentready-webhook-tick": {"task": "agentready.webhook_tick", "schedule": 10.0}},
    timezone="UTC",
)


from celery.signals import after_setup_logger, after_setup_task_logger  # noqa: E402


@after_setup_logger.connect
@after_setup_task_logger.connect
def _install_log_redaction(logger=None, **_):
    """Celery configures its own handlers: add secret redaction (and JSON formatting) to them."""
    from app.core.observability import RedactSecretsFilter, RequestIdFilter, configure_logging

    configure_logging(settings)
    if logger is not None:
        for h in logger.handlers:
            h.addFilter(RequestIdFilter())
            h.addFilter(RedactSecretsFilter())


def _run(coro_factory):
    from app.db import session as db_session

    async def _main():
        engine = db_session.set_engine(settings.database_url)  # asyncpg connections are bound to an event loop
        try:
            return await coro_factory()
        finally:
            await engine.dispose()

    return asyncio.run(_main())


@celery_app.task(name="agentready.scheduler_tick", ignore_result=True)
def scheduler_tick_task() -> None:
    from app.workers.scheduler import tick

    _run(tick)


@celery_app.task(name="agentready.webhook_tick", ignore_result=True)
def webhook_tick_task() -> None:
    from app.workers.scheduler import webhook_tick

    _run(webhook_tick)


@celery_app.task(name="agentready.generate_feed", bind=True, max_retries=2, acks_late=True)
def generate_feed_task(self, snapshot_id: str) -> None:
    from app.db.session import session_factory
    from app.services.distribution.feeds import run_snapshot

    async def go():
        async with session_factory()() as s:
            await run_snapshot(s, uuid.UUID(snapshot_id))  # claims queued -> running: never runs twice

    _run(go)


@celery_app.task(name="agentready.run_bulk_operation", bind=True, max_retries=3, acks_late=True)
def run_bulk_task(self, op_id: str) -> None:
    from app.db.session import session_factory
    from app.services.commerce.bulk import run

    async def go():
        async with session_factory()() as s:
            await run(s, uuid.UUID(op_id))  # idempotent: only pending items are processed on retry

    try:
        _run(go)
    except Exception as e:  # pragma: no cover - transient DB/broker errors
        raise self.retry(exc=e, countdown=10)


@celery_app.task(name="agentready.run_integration_sync", ignore_result=True)
def run_integration_sync_task(job_id: str) -> None:
    from app.workers.integration_jobs import run_queued_import

    _run(lambda: run_queued_import(uuid.UUID(job_id)))


@celery_app.task(name="agentready.run_scan")
def run_scan_task(scan_id: str) -> None:
    from app.db import session as db_session
    from app.workers.scan_runner import run_scan

    async def _main():
        # fresh engine per task: asyncpg connections are bound to an event loop
        engine = db_session.set_engine(settings.database_url)
        try:
            await run_scan(uuid.UUID(scan_id))
        finally:
            await engine.dispose()

    asyncio.run(_main())
