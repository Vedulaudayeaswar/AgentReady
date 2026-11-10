"""Background execution of integration imports (catalog imports can be large)."""

from __future__ import annotations

import asyncio
import uuid

from app.core.config import get_settings
from app.db.session import session_factory
from app.models import CommerceIntegration, SyncJob
from app.services.commerce.audit import Actor


async def run_queued_import(job_id: uuid.UUID) -> None:
    from app.services.integrations.service import run_import

    async with session_factory()() as db:
        job = await db.get(SyncJob, job_id)
        if job is None or job.status != "queued":
            return  # already running/finished (duplicate delivery of the task)
        integration = await db.get(CommerceIntegration, job.integration_id)
        if integration is None:
            job.status = "failed"
            await db.commit()
            return
        actor = Actor("user", job.created_by, "Integration import") if job.created_by else Actor.system("Integration import")
        await run_import(db, integration, actor, trigger=job.trigger, job=job)


def dispatch_import(job_id: uuid.UUID) -> str:
    if get_settings().job_backend == "celery":
        from app.workers.celery_app import run_integration_sync_task

        run_integration_sync_task.delay(str(job_id))
        return "celery"
    from app.workers.dispatch import _inline_tasks

    task = asyncio.get_running_loop().create_task(run_queued_import(job_id))
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)
    return "inline"
