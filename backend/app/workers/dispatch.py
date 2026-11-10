"""Dispatch scans to Celery or run them inline (in-process asyncio task)."""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.core.config import get_settings

log = logging.getLogger("agentready.dispatch")
_inline_tasks: set[asyncio.Task] = set()


def dispatch_scan(scan_id: uuid.UUID) -> str:
    s = get_settings()
    if s.job_backend == "celery":
        from app.workers.celery_app import run_scan_task

        run_scan_task.delay(str(scan_id))
        return "celery"
    from app.workers.scan_runner import run_scan

    task = asyncio.get_running_loop().create_task(run_scan(scan_id))
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)
    return "inline"


async def wait_for_inline_tasks() -> None:
    """Testing helper: wait until inline scans complete."""
    while _inline_tasks:
        await asyncio.gather(*list(_inline_tasks), return_exceptions=True)
