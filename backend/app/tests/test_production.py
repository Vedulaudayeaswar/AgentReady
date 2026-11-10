"""Phase 5: production readiness — configuration validation, real health checks, request limits, security
headers, log redaction, login throttling, reset-token invalidation, interrupted-job recovery, scheduler
stage isolation, the merchant system-status page and a cross-surface private-data leakage check."""

import json
import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core import ratelimit
from app.core.config import Settings, production_problems
from app.core.observability import JsonFormatter, RedactSecretsFilter, redact
from app.models import Business, BulkOperation, BulkOperationItem, FeedConfiguration, FeedSnapshot, ScanJob
from app.services.commerce.money import now_utc
from app.services.distribution import cache, feeds
from app.tests.conftest import register

API = "/api/v1"
V3 = "/api/v3/agents"
FERNET = "y0ZQh4vGQyQ1gqJfV8r3o0bQJ7m3kz4Kx0p2m8Y3r1U="


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------- configuration
def test_production_configuration_is_validated():
    unsafe = Settings(environment="production", secret_key="short", database_url="sqlite+aiosqlite:///x.db",
                      job_backend="inline", ai_provider="mock", cors_origins="*", allow_private_networks=True,
                      credentials_encryption_key="")
    problems = " | ".join(production_problems(unsafe))
    for needle in ("SECRET_KEY", "ALLOW_PRIVATE_NETWORKS", "CREDENTIALS_ENCRYPTION_KEY", "AI_PROVIDER=mock",
                   "CORS_ORIGINS", "JOB_BACKEND", "PostgreSQL"):
        assert needle in problems, needle
    assert "not a valid Fernet key" in " ".join(production_problems(unsafe.model_copy(
        update={"credentials_encryption_key": "not-a-key"})))
    safe = Settings(environment="production", secret_key="x" * 48, credentials_encryption_key=FERNET,
                    database_url="postgresql+asyncpg://u:p@db/agentready", job_backend="celery",
                    cors_origins="https://shop.example")
    assert production_problems(safe) == []
    assert safe.docs_enabled is False and Settings(environment="development").docs_enabled is True


# ---------------------------------------------------------------- health
async def test_health_reports_real_dependency_status(client, settings):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    r = await client.get("/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok" and body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "not_used" and "latency_ms" in body["checks"]["database"]
    # Celery mode with Redis unreachable: unavailable, 503 (never a hard-coded "ok")
    settings.job_backend, settings.redis_url = "celery", "redis://127.0.0.1:1/0"
    r = await client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"
    assert r.json()["checks"]["redis"]["status"] == "error"


async def test_database_outage_returns_503_not_500(tmp_path, settings):
    from app.db import session as db_session
    from app.main import app

    db_session.set_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing-dir' / 'x.db'}")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://test") as c:
            r = await c.get("/health")
            assert r.status_code == 503 and r.json()["checks"]["database"]["status"] == "error"
            r = await c.post(f"{API}/auth/login", json={"email": "a@example.com", "password": "whatever-123"})
            assert r.status_code == 503, r.text
            err = r.json()["error"]
            assert err["code"] == "service_unavailable" and err["details"]["request_id"]
            assert r.headers["retry-after"] == "5" and "sqlite" not in r.text.lower()
    finally:
        await db_session.get_engine().dispose()


# ---------------------------------------------------------------- HTTP hardening
async def test_request_body_limit_headers_and_request_id(client, settings):
    settings.max_request_body_bytes = 10_000
    big = {"email": "a@example.com", "password": "x" * 20_000}
    r = await client.post(f"{API}/auth/login", json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    async def chunks():  # no Content-Length: counted while streaming
        for _ in range(5):
            yield b"x" * 4000

    r = await client.post(f"{API}/auth/login", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    r = await client.get("/health/live", headers={"X-Request-ID": "trace-12345678"})
    assert r.headers["x-request-id"] == "trace-12345678"
    r = await client.get(f"{API}/auth/me", headers={"X-Request-ID": "bad id!"})
    assert r.status_code == 401 and len(r.headers["x-request-id"]) == 16  # invalid ids are replaced
    for h in ("x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy",
              "cross-origin-opener-policy", "server-timing"):
        assert h in r.headers, h
    assert r.headers["cache-control"] == "no-store"


async def test_unhandled_error_returns_request_id_without_internals(settings):
    from app.main import app

    async def boom():
        raise RuntimeError("secret internal detail")

    app.add_api_route("/__boom", boom)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://test") as c:
            r = await c.get("/__boom", headers={"X-Request-ID": "req-abcdef12"})
        assert r.status_code == 500
        assert r.json()["error"] == {"code": "internal_error", "message": "An unexpected error occurred",
                                     "details": {"request_id": "req-abcdef12"}}
        assert "secret internal detail" not in r.text
    finally:
        app.router.routes = [rt for rt in app.router.routes if getattr(rt, "path", "") != "/__boom"]


def test_logs_redact_secrets_and_json_format():
    samples = {
        "GET /api/v3/feeds/arf_AbCdEf123456789_xyz/download": "arf_AbCdEf",
        "key ar_cat_Zz9Yy8Xx7Ww6Vv5Uu4": "ar_cat_Zz9",
        "secret whsec_QUJDREVGR0hJSktMTU5PUFFSU1RVVldY": "whsec_QUJD",
        "groq gsk_live1234567890abcdef": "gsk_live",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig": "eyJhbGci",
        "https://app.example/reset-password?token=Sup3rS3cretT0ken&x=1": "Sup3rS3cret",
    }
    for raw, secret in samples.items():
        assert secret not in redact(raw) and "[redacted]" in redact(raw), raw
    rec = logging.LogRecord("agentready.test", logging.INFO, __file__, 1, "feed %s served", ("/api/v3/feeds/arf_" + "q" * 30,), None)
    RedactSecretsFilter().filter(rec)
    rec.request_id = "rid-1234"
    rec.duration_ms = 12.5
    out = json.loads(JsonFormatter().format(rec))
    assert out["message"] == "feed /api/v3/feeds/[redacted] served" and out["request_id"] == "rid-1234"
    assert out["duration_ms"] == 12.5 and out["level"] == "INFO" and out["logger"] == "agentready.test"


# ---------------------------------------------------------------- authentication
async def test_failed_logins_are_throttled_per_account(client, settings):
    await register(client)
    settings.login_failures_per_account = 4
    ratelimit.reset_rate_limits()
    bad = {"email": "OWNER@example.com", "password": "wrong-password-1"}
    codes = [(await client.post(f"{API}/auth/login", json=bad)).status_code for _ in range(4)]
    assert codes == [401] * 4
    r = await client.post(f"{API}/auth/login", json={"email": "owner@example.com", "password": "correct-horse-42"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_failed_logins"
    # unknown emails are throttled identically (no account enumeration)
    ghost = {"email": "ghost@example.com", "password": "nope-nope-1"}
    assert [(await client.post(f"{API}/auth/login", json=ghost)).status_code for _ in range(5)] == [401] * 4 + [429]
    # other accounts are unaffected
    await register(client, email="second@example.com", business="Second")
    ok = await client.post(f"{API}/auth/login", json={"email": "second@example.com", "password": "correct-horse-42"})
    assert ok.status_code == 200


async def test_password_reset_invalidates_other_reset_links_and_clears_throttle(client, settings):
    await register(client)
    settings.environment = "development"
    links = [(await client.post(f"{API}/auth/forgot-password", json={"email": "owner@example.com"})).json()["dev_reset_url"]
             for _ in range(2)]
    t1, t2 = (u.split("token=")[1] for u in links)
    settings.login_failures_per_account = 2
    for _ in range(2):
        await client.post(f"{API}/auth/login", json={"email": "owner@example.com", "password": "bad-guess-000"})
    assert (await client.post(f"{API}/auth/reset-password", json={"token": t2, "password": "fresh-pass-2026"})).status_code == 200
    r = await client.post(f"{API}/auth/reset-password", json={"token": t1, "password": "other-pass-2026"})
    assert r.status_code == 400  # the older link no longer works
    r = await client.post(f"{API}/auth/login", json={"email": "owner@example.com", "password": "fresh-pass-2026"})
    assert r.status_code == 200  # the owner is not locked out after resetting


# ---------------------------------------------------------------- background jobs
async def _business(db) -> Business:
    return (await db.execute(select(Business))).scalars().first()


async def test_interrupted_jobs_are_recovered_and_reported(client, db, settings):
    from app.models import Website
    from app.workers.recovery import recover_stale_jobs
    from app.workers.scheduler import housekeeping

    h = await register(client)
    other = await register(client, email="other@example.com", business="Other")
    b = await _business(db)
    old = now_utc() - timedelta(hours=2)
    feed = FeedConfiguration(business_id=b.id, name="Nightly feed", format="json")
    db.add(feed)
    await db.flush()
    db.add(FeedSnapshot(business_id=b.id, feed_id=feed.id, version=1, status="running", created_at=old, started_at=old))
    db.add(FeedSnapshot(business_id=b.id, feed_id=feed.id, version=2, status="running", created_at=now_utc(),
                        started_at=now_utc()))  # still within the window: untouched
    site = Website(business_id=b.id, url="https://shop.example", normalized_url="https://shop.example", domain="shop.example")
    db.add(site)
    await db.flush()
    db.add(ScanJob(business_id=b.id, website_id=site.id, status="running", stage="extracting", updated_at=old,
                   created_at=old))
    op = BulkOperation(business_id=b.id, operation="approve", status="running", total=2, run_mode="background",
                       result={"recovery_attempts": 1}, updated_at=old, created_at=old)
    db.add(op)
    await db.flush()
    db.add_all([BulkOperationItem(operation_id=op.id, business_id=b.id, status="succeeded"),
                BulkOperationItem(operation_id=op.id, business_id=b.id, status="pending")])
    op.succeeded = 1
    await db.commit()
    # Timestamps.updated_at has onupdate=now: age the row explicitly, as a stuck worker would leave it
    await db.execute(update(BulkOperation).where(BulkOperation.id == op.id).values(updated_at=old))
    await db.commit()

    out = await housekeeping(db)
    assert out["feed_snapshots_failed"] == 1 and out["scans_failed"] == 1 and out["bulk_failed"] == 1
    snaps = {s.version: s for s in (await db.execute(select(FeedSnapshot).execution_options(populate_existing=True))).scalars()}
    assert snaps[1].status == "failed" and "Interrupted" in snaps[1].error and snaps[2].status == "running"
    await db.refresh(op)
    assert op.status == "completed_with_errors" and op.failed == 1 and "1 item(s) were not processed" in op.error
    assert (await recover_stale_jobs(db)) == {k: 0 for k in out if k in (
        "scans_failed", "feed_snapshots_failed", "bulk_resumed", "bulk_failed", "syncs_failed")}  # idempotent

    st = (await client.get(f"{API}/system/status", headers=h)).json()
    kinds = {f["kind"] for f in st["recent_failures"]}
    assert {"feed_generation", "scan", "bulk_operation"} <= kinds
    assert st["jobs"]["feed_generation"] == {"failed": 1, "running": 1} and st["active_jobs"] == 1
    assert st["health"]["checks"]["database"]["status"] == "ok"
    assert all(f["retry"] and f["link"].startswith("/app/") for f in st["recent_failures"])
    other_st = (await client.get(f"{API}/system/status", headers=other)).json()
    assert other_st["recent_failures"] == [] and other_st["active_jobs"] == 0  # tenant isolated
    assert (await client.get(f"{API}/system/status")).status_code == 401


async def test_interrupted_bulk_operation_is_resumed_once(client, db, monkeypatch):
    from app.services.commerce import bulk
    from app.workers.recovery import recover_stale_jobs

    await register(client)
    b = await _business(db)
    op = BulkOperation(business_id=b.id, operation="approve", status="running", total=1, run_mode="background",
                       result={}, updated_at=now_utc() - timedelta(hours=1))
    db.add(op)
    await db.commit()
    resumed = []
    monkeypatch.setattr(bulk, "dispatch", lambda op_id: resumed.append(op_id) or "inline")
    out = await recover_stale_jobs(db)
    assert out["bulk_resumed"] == 1 and resumed == [op.id]
    await db.refresh(op)
    assert op.status == "running" and op.result["recovery_attempts"] == 1


async def test_failing_scheduler_stage_does_not_block_the_others(client, db, monkeypatch):
    from app.services.commerce import pricing
    from app.services.distribution import feeds as feeds_mod
    from app.workers import scheduler

    async def broken(_db):
        raise RuntimeError("boom")

    seen = []

    async def feeds_ok(_db):
        seen.append("feeds")
        return {"generated": 0}

    monkeypatch.setattr(pricing, "apply_due_price_changes", broken)
    monkeypatch.setattr(feeds_mod, "generate_due_feeds", feeds_ok)
    out = await scheduler.tick()
    assert out["prices"] == {"error": "RuntimeError"} and seen == ["feeds"]
    assert "housekeeping" in out and "intelligence" in out and "error" not in out["intelligence"]


# ---------------------------------------------------------------- private data never leaves through public surfaces
async def test_private_merchant_data_never_reaches_public_surfaces(client):
    h = await register(client)
    r = await client.post(f"{API}/products", headers=h, json={
        "name": "Sentinel Drill", "regular_price": "2500.00", "currency": "INR", "lifecycle_status": "active",
        "sku": "DRL-1", "brand": "Bosch", "description": "Cordless drill", "cost_price": "98765.43",
        "attributes": [{"label": "Supplier", "value": "SENTINEL-SUPPLIER"}, {"label": "Voltage", "value": "18 V"}],
        "track_inventory": True, "stock_quantity": 4321})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert (await client.patch(f"{API}/products/{pid}", json={"approval_status": "approved"}, headers=h)).status_code == 200
    r = await client.post(f"{API}/inventory/adjust", headers=h, json={"product_id": pid, "operation": "increase",
                                                                   "quantity": 1, "reason": "SENTINEL-NOTE restock"})
    assert r.status_code in (200, 201), r.text
    r = await client.put(f"{API}/distribution/settings", headers=h, json={
        "enabled": True, "public_name": "Sentinel Tools", "public_url": "https://sentinel.example", "public_network": True})
    assert r.status_code == 200, r.text
    key = (await client.post(f"{API}/api-keys", headers=h, json={"name": "Agent", "scopes": ["agent:read", "catalog:read"]})).json()
    k = {"X-Catalog-Key": key["key"]}

    bodies = {
        "search": await client.get(f"{V3}/search?q=drill", headers=k),
        "detail": await client.get(f"{V3}/products/{pid}", headers=k),
        "pricing": await client.get(f"{V3}/products/{pid}/pricing", headers=k),
        "availability": await client.get(f"{V3}/products/{pid}/availability", headers=k),
        "v2": await client.get("/api/v2/catalog/products", headers=k),
        "v2_item": await client.get(f"/api/v2/catalog/products/{pid}", headers=k),
        "v1": await client.get("/api/v1/catalog", headers=k),
        "v1_jsonld": await client.get(f"/api/v1/catalog/{pid}?format=jsonld", headers=k),
        "well_known": await client.get("/.well-known/agentready.json"),
    }
    mcp_hdr = {**k, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    for name, args in (("search_products", {"query": "drill"}), ("get_product_details", {"product_id": pid}),
                       ("get_product_pricing", {"product_id": pid}), ("check_product_availability", {"product_id": pid})):
        bodies[f"mcp:{name}"] = await client.post("/api/v3/mcp", headers=mcp_hdr, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    for fmt in ("json", "csv", "jsonld", "xml", "jsonl"):
        f = (await client.post(f"{API}/distribution/feeds", json={"name": f"Feed {fmt}", "format": fmt}, headers=h)).json()
        tok = (await client.post(f"{API}/distribution/feeds/{f['id']}/tokens", json={"name": "Partner"}, headers=h)).json()
        await client.post(f"{API}/distribution/feeds/{f['id']}/generate", headers=h)
        await feeds.wait_for_inline()
        bodies[f"feed:{fmt}"] = await client.get(httpx.URL(tok["url"]).raw_path.decode())
    for name, resp in bodies.items():
        assert resp.status_code == 200, (name, resp.status_code, resp.text[:300])
        text = resp.text
        for secret in ("98765", "SENTINEL-SUPPLIER", "Supplier", "4321", "4322", "SENTINEL-NOTE", "cost_price",
                       "approval_status", "manual_overrides", "stock_quantity"):
            assert secret not in text, (name, secret)
        if name != "well_known":  # the capability document states the policy "...evidence are never returned"
            assert "evidence" not in text, name
    assert "Sentinel Drill" in bodies["search"].text and "Sentinel Drill" in bodies["feed:csv"].text
    assert "2500" in bodies["pricing"].text


# ---------------------------------------------------------------- intelligence scheduling (Phase 4 regression found in Phase 5)
async def test_daily_notification_frequency_still_notifies_and_never_skips_changes(client, db):
    from app.models import IntelligenceSettings, MerchantNotification
    from app.services.intelligence import notifications

    h = await register(client)
    r = await client.post(f"{API}/products", headers=h, json={"name": "Widget", "regular_price": "100", "currency": "INR",
                                                             "lifecycle_status": "active", "sku": "W"})
    pid = r.json()["id"]
    await client.put(f"{API}/intelligence/settings", json={"notification_frequency": "daily"}, headers=h)
    b = await _business(db)
    await notifications.run_for_business(db, b)  # first pass: alerts evaluated, last_notified_at set
    for price in ("110", "120"):
        await client.put(f"{API}/products/{pid}/pricing", json={"regular_price": price}, headers=h)
    # 10-minute passes during the day do not send alerts (daily digest) ...
    db.expire_all()
    out = await notifications.run_for_business(db, await _business(db))
    assert out["notifications"] == 0
    assert (await db.execute(select(MerchantNotification).where(MerchantNotification.kind == "price_change"))
            ).scalars().all() == []
    # ... and once 24 h have passed since the last alert evaluation, the changes made in between are reported
    await db.execute(update(IntelligenceSettings).values(last_notified_at=now_utc() - timedelta(hours=25)))
    await db.commit()
    db.expire_all()
    await notifications.run_for_business(db, await _business(db))
    row = (await db.execute(select(MerchantNotification).where(MerchantNotification.kind == "price_change"))).scalar_one()
    assert row.count == 2


async def test_concurrent_intelligence_ticks_do_not_evaluate_a_merchant_twice(client, db_engine, monkeypatch):
    import asyncio

    from app.db.session import session_factory
    from app.services.intelligence import notifications

    h = await register(client)
    await client.post(f"{API}/products", headers=h, json={"name": "Widget", "regular_price": "100", "currency": "INR",
                                                         "lifecycle_status": "active"})
    runs = []
    real = notifications.run_for_business

    async def counting(db, business, force=False):
        runs.append(business.id)
        return await real(db, business, force)

    monkeypatch.setattr(notifications, "run_for_business", counting)

    async def one_tick():
        async with session_factory()() as s:
            return await notifications.tick(s)

    results = await asyncio.gather(one_tick(), one_tick())
    assert sum(r["merchants"] for r in results) == 1 and len(runs) == 1
