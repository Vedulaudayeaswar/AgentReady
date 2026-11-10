"""Small sliding-window rate limiter.

Uses Redis when JOB_BACKEND=celery (Redis is then required anyway) and an
in-process store otherwise. The in-process store is per-worker; that is
acceptable for single-process development, not for multi-replica production.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.core.config import get_settings

_windows: dict[str, deque[float]] = defaultdict(deque)
_redis = None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _hit_redis(key: str, limit: int, window: int) -> bool:
    global _redis
    import redis.asyncio as aioredis

    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url)
    bucket = f"rl:{key}:{int(time.time() // window)}"
    count = await _redis.incr(bucket)
    if count == 1:
        await _redis.expire(bucket, window)
    return count <= limit


def _hit_memory(key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    q = _windows[key]
    while q and q[0] <= now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


async def hit(key: str, limit: int, window: int = 60) -> bool:
    s = get_settings()
    try:
        return await _hit_redis(key, limit, window) if s.job_backend == "celery" else _hit_memory(key, limit, window)
    except Exception:
        return _hit_memory(key, limit, window)


def reset_rate_limits() -> None:
    _windows.clear()
    _failures.clear()


def rate_limit(scope: str, limit: int | None = None, window: int = 60):
    async def dependency(request: Request) -> None:
        s = get_settings()
        lim = limit or s.auth_rate_limit_per_minute
        key = f"{scope}:{_client_ip(request)}"
        try:
            allowed = await _hit_redis(key, lim, window) if s.job_backend == "celery" else _hit_memory(key, lim, window)
        except Exception:  # Redis unavailable: degrade to memory rather than fail open
            allowed = _hit_memory(key, lim, window)
        if not allowed:
            raise HTTPException(429, "Too many requests, please slow down", headers={"Retry-After": str(window)})

    return dependency


# ---- failure counters (per-account login throttle) ------------------------------------------------
_failures: dict[str, deque[float]] = defaultdict(deque)


async def _redis_client():
    global _redis
    import redis.asyncio as aioredis

    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url)
    return _redis


async def failure_count(key: str, window: int) -> int:
    if get_settings().job_backend == "celery":
        try:
            r = await _redis_client()
            return int(await r.get(f"fail:{key}") or 0)
        except Exception:
            pass
    q = _failures[key]
    now = time.monotonic()
    while q and q[0] <= now - window:
        q.popleft()
    return len(q)


async def record_failure(key: str, window: int) -> None:
    if get_settings().job_backend == "celery":
        try:
            r = await _redis_client()
            n = await r.incr(f"fail:{key}")
            if n == 1:
                await r.expire(f"fail:{key}", window)
            return
        except Exception:
            pass
    _failures[key].append(time.monotonic())


async def clear_failures(key: str) -> None:
    _failures.pop(key, None)
    if get_settings().job_backend == "celery":
        try:
            await (await _redis_client()).delete(f"fail:{key}")
        except Exception:
            pass
