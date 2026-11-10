"""Short-lived cache for public (agent-facing) responses, invalidated by catalog changes.

* Every cached entry is keyed by the merchant's *catalog generation*. Any committed change to a product,
  variant or attribute (price, stock, publication, distribution settings, ...) bumps the generation of that
  merchant (see ``events``), so the next request misses the cache.
* Entries also expire after ``TTL_SECONDS`` and at the next sale-window boundary (``valid_until``), so a
  price never stays cached past the moment it changes, even without a database write.
* With JOB_BACKEND=celery the generation counter lives in Redis so changes made by the worker invalidate the
  API's cache; with the inline backend it is in-process.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from app.core.config import get_settings

log = logging.getLogger("agentready.cache")
TTL_SECONDS = 30
MAX_ENTRIES = 2000

_local_gen: dict[str, int] = {}
_entries: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()
_redis = None
stats = {"hits": 0, "misses": 0, "invalidations": 0}


def _client():
    global _redis
    if get_settings().job_backend != "celery":
        return None
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _redis


def generation(business_id) -> int:
    key = str(business_id)
    r = _client()
    if r is not None:
        try:
            return int(r.get(f"ar:catgen:{key}") or 0)
        except Exception:  # Redis unavailable: never serve from cache
            return -int(time.time() * 1000)
    return _local_gen.get(key, 0)


def bump(business_ids) -> None:
    ids = {str(b) for b in business_ids if b is not None}
    if not ids:
        return
    stats["invalidations"] += len(ids)
    with _lock:
        for k in ids:
            _local_gen[k] = _local_gen.get(k, 0) + 1
        # drop local entries for these merchants immediately
        for ck in [ck for ck in _entries if any(f"|{k}:" in ck for k in ids)]:
            _entries.pop(ck, None)
    r = _client()
    if r is not None:
        try:
            pipe = r.pipeline()
            for k in ids:
                pipe.incr(f"ar:catgen:{k}")
            pipe.execute()
        except Exception:
            log.warning("could not bump catalog generation in Redis; cached public responses expire in %ss", TTL_SECONDS)


def make_key(namespace: str, business_ids, params: dict) -> str:
    gens = ",".join(f"|{b}:{generation(b)}" for b in sorted(str(x) for x in business_ids)) + f"|*:{generation('*')}"
    return f"{namespace}{gens}#{json.dumps(params, sort_keys=True, default=str)}"


def get(key: str) -> dict | None:
    now = time.time()
    with _lock:
        hit = _entries.get(key)
        if hit is None or hit[0] <= now:
            if hit is not None:
                _entries.pop(key, None)
            stats["misses"] += 1
            return None
    stats["hits"] += 1
    return json.loads(hit[1])


def put(key: str, value: dict, valid_until: str | None = None) -> None:
    expires = time.time() + TTL_SECONDS
    if valid_until:
        try:
            vu = datetime.fromisoformat(valid_until).astimezone(timezone.utc).timestamp()
            expires = min(expires, vu)
        except ValueError:
            pass
    if expires <= time.time():
        return
    with _lock:
        if len(_entries) >= MAX_ENTRIES:
            for k in sorted(_entries, key=lambda k: _entries[k][0])[: MAX_ENTRIES // 4]:
                _entries.pop(k, None)
        _entries[key] = (expires, json.dumps(value, default=str))


def clear() -> None:
    with _lock:
        _entries.clear()
        _local_gen.clear()
