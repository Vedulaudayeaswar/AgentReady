"""Dependency health checks. Every check performs a real round trip; nothing is assumed healthy.

* database  - ``SELECT 1`` through the application's connection pool
* redis     - ``PING`` (only when JOB_BACKEND=celery: Redis is then the broker and the rate-limit store)
* worker    - Celery control ``ping`` broadcast; at least one worker must answer (result cached 15 s)
* scheduler - age of the heartbeat the scheduler tick writes to Redis at the start of every run

Overall status: ``ok`` (everything required answered), ``degraded`` (API serves requests but background
work is not running: no worker / stale scheduler), ``unavailable`` (database or Redis down; HTTP 503).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import get_settings

SCHEDULER_HEARTBEAT_KEY = "agentready:scheduler:last_tick"
SCHEDULER_STALE_SECONDS = 180
_worker_cache: dict = {"at": 0.0, "value": None}


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def check_database() -> dict:
    from app.db.session import get_engine

    start = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=5)
        return {"status": "ok", "latency_ms": _ms(start)}
    except Exception as e:  # noqa: BLE001 - report, never raise
        return {"status": "error", "latency_ms": _ms(start), "error": type(e).__name__}


async def _redis():
    import redis.asyncio as aioredis

    return aioredis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)


async def check_redis() -> dict:
    start = time.perf_counter()
    try:
        r = await _redis()
        try:
            await r.ping()
        finally:
            await r.aclose()
        return {"status": "ok", "latency_ms": _ms(start)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "latency_ms": _ms(start), "error": type(e).__name__}


def _ping_workers() -> dict:
    from app.workers.celery_app import celery_app

    start = time.perf_counter()
    try:
        replies = celery_app.control.ping(timeout=1.5) or []
        names = sorted(n for r in replies for n in r)
        if not names:
            return {"status": "error", "latency_ms": _ms(start), "workers": 0, "error": "no worker answered"}
        return {"status": "ok", "latency_ms": _ms(start), "workers": len(names)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "latency_ms": _ms(start), "workers": 0, "error": type(e).__name__}


async def check_worker() -> dict:
    now = time.monotonic()
    if _worker_cache["value"] is not None and now - _worker_cache["at"] < 15:
        return {**_worker_cache["value"], "cached": True}
    value = await asyncio.to_thread(_ping_workers)
    _worker_cache.update(at=now, value=value)
    return value


async def record_scheduler_heartbeat() -> None:
    """Called by every scheduler tick (Celery Beat -> worker). Best effort."""
    s = get_settings()
    if s.job_backend != "celery":
        return
    try:
        r = await _redis()
        try:
            await r.set(SCHEDULER_HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat(), ex=24 * 3600)
        finally:
            await r.aclose()
    except Exception:  # noqa: BLE001 - health reports the missing heartbeat
        pass


async def check_scheduler() -> dict:
    try:
        r = await _redis()
        try:
            raw = await r.get(SCHEDULER_HEARTBEAT_KEY)
        finally:
            await r.aclose()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": type(e).__name__}
    if raw is None:
        return {"status": "error", "error": "no scheduler tick recorded in the last 24 hours"}
    last = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    status = "ok" if age <= SCHEDULER_STALE_SECONDS else "error"
    out = {"status": status, "last_tick_at": last.isoformat(), "age_seconds": round(age)}
    if status != "ok":
        out["error"] = f"last tick {round(age)}s ago"
    return out


async def run_checks() -> tuple[int, dict]:
    s = get_settings()
    checks: dict[str, dict] = {"database": await check_database()}
    if s.job_backend == "celery":
        checks["redis"] = await check_redis()
        if checks["redis"]["status"] == "ok":
            if s.health_check_worker:
                checks["worker"] = await check_worker()
            checks["scheduler"] = await check_scheduler()
    else:
        checks["redis"] = {"status": "not_used", "detail": "JOB_BACKEND=inline: jobs run inside the API process"}
    critical_down = checks["database"]["status"] != "ok" or checks["redis"]["status"] == "error"
    background_down = any(checks.get(k, {}).get("status") == "error" for k in ("worker", "scheduler"))
    status = "unavailable" if critical_down else ("degraded" if background_down else "ok")
    body = {"status": status, "environment": s.environment, "ai_provider": s.ai_provider,
            "ai_configured": s.ai_enabled, "job_backend": s.job_backend, "checks": checks,
            "checked_at": datetime.now(timezone.utc).isoformat()}
    return (503 if critical_down else 200), body
