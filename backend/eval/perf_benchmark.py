"""Performance benchmark against a large catalog in a DISPOSABLE PostgreSQL database.

    PERF_DATABASE_URL=postgresql+asyncpg://agentready:agentready@localhost:55432/agentready_perf \
        python eval/perf_benchmark.py --products 5000

The database named in PERF_DATABASE_URL is created if missing, migrated with Alembic and filled with
synthetic products (clearly named "Perf product N"). It must not be the live database: the script refuses
names without "perf" in them. Requests run in-process through the real ASGI app (no network), so the
numbers measure the application + database, not nginx. Results are printed and written to
eval/results/perf-<n>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _prepare_env(url: str) -> None:
    db = url.rsplit("/", 1)[1]
    if "perf" not in db:
        sys.exit("refusing: PERF_DATABASE_URL must name a disposable database containing 'perf'")
    os.environ.update({"DATABASE_URL": url, "ENVIRONMENT": "test", "JOB_BACKEND": "inline", "AI_PROVIDER": "none",
                       "SECRET_KEY": "perf-benchmark-secret-key-0123456789abcdef", "AUTH_RATE_LIMIT_PER_MINUTE": "100000",
                       "API_KEY_DEFAULT_RATE_LIMIT": "100000", "LOG_LEVEL": "WARNING"})


async def _create_db(url: str) -> None:
    import asyncpg

    base, db = url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)
    conn = await asyncpg.connect(base + "/postgres")
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", db):
            await conn.execute(f'DROP DATABASE "{db}"')
        await conn.execute(f'CREATE DATABASE "{db}"')
    finally:
        await conn.close()


async def _seed(client, n: int) -> dict:
    from sqlalchemy import select

    from app.db.session import session_factory
    from app.models import Business, Product, ProductAttribute

    r = await client.post("/api/v1/auth/register", json={"email": "perf@example.com", "password": "perf-pass-2026x",
                                                          "full_name": "Perf", "business_name": "Perf Store"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    brands = ["Bosch", "Havells", "Zenon", "Acme", "Makita", "Philips"]
    cats = ["Power tools", "Motors", "Laptops", "Lighting", "Kitchen", "Garden"]
    t0 = time.perf_counter()
    async with session_factory()() as s:
        b = (await s.execute(select(Business))).scalars().first()
        for start in range(0, n, 500):
            for i in range(start, min(n, start + 500)):
                p = Product(business_id=b.id, match_key=f"perf-{i}", name=f"Perf product {i} {cats[i % 6]}",
                            sku=f"PERF-{i:05d}", brand=brands[i % 6], category=cats[i % 6],
                            description=("Synthetic benchmark product. " * 6).strip() if i % 3 else None,
                            price_amount=Decimal(100 + i % 900), currency="INR", availability="InStock",
                            product_url=f"https://perf.example/p/{i}", images=[{"url": f"https://perf.example/i/{i}.jpg"}],
                            approval_status="approved", publication_status="published", lifecycle_status="active",
                            origin="manual", quality_status="valid", cost_price_amount=Decimal(50))
                s.add(p)
                await s.flush()
                s.add_all([ProductAttribute(product_id=p.id, key="voltage", label="Voltage", value=str(12 + i % 220), unit="V"),
                           ProductAttribute(product_id=p.id, key="weight", label="Weight", value=str(1 + i % 9), unit="kg")])
            await s.commit()
    seed_s = time.perf_counter() - t0
    r = await client.put("/api/v1/distribution/settings", headers=h, json={"enabled": True, "public_name": "Perf Store",
                                                                         "public_url": "https://perf.example"})
    assert r.status_code == 200, r.text
    key = (await client.post("/api/v1/api-keys", headers=h, json={"name": "Perf agent", "scopes": ["agent:read"],
                                                                   "rate_limit_per_minute": 10000})).json()["key"]
    return {"h": h, "k": {"X-Catalog-Key": key}, "seed_seconds": round(seed_s, 1)}


async def _time(fn, runs: int) -> dict:
    samples = []
    status = None
    for _ in range(runs):
        t = time.perf_counter()
        r = await fn()
        samples.append((time.perf_counter() - t) * 1000)
        status = getattr(r, "status_code", status)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return {"p50_ms": round(statistics.median(samples), 1), "p95_ms": round(p95, 1), "max_ms": round(samples[-1], 1),
            "runs": runs, "status": status}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=5000)
    ap.add_argument("--runs", type=int, default=15)
    args = ap.parse_args()
    url = os.environ.get("PERF_DATABASE_URL") or sys.exit("set PERF_DATABASE_URL")
    _prepare_env(url)
    await _create_db(url)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True,
                   env={**os.environ, "DATABASE_URL": url})

    import httpx

    from app.main import app
    from app.services.distribution import feeds

    out: dict = {"products": args.products}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://perf", timeout=600) as c:
        ctx = await _seed(c, args.products)
        h, k = ctx["h"], ctx["k"]
        out["seed_seconds"] = ctx["seed_seconds"]
        some = (await c.get("/api/v1/products?page_size=1", headers=h)).json()["items"][0]["id"]
        mcp_h = {**k, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        cases = {
            "catalog list (page 1, 50)": lambda: c.get("/api/v1/products?page_size=50", headers=h),
            "catalog list (page 60, 50)": lambda: c.get("/api/v1/products?page=60&page_size=50", headers=h),
            "catalog search q=motor": lambda: c.get("/api/v1/products?q=motors&page_size=50", headers=h),
            "dashboard overview": lambda: c.get("/api/v1/dashboard/overview", headers=h),
            "dashboard charts": lambda: c.get("/api/v1/dashboard/charts", headers=h),
            "agent search q=motor": lambda: c.get("/api/v3/agents/search?q=motor", headers=k),
            "agent search + attribute filter": lambda: c.get("/api/v3/agents/search?q=motor&attr=voltage:230 V", headers=k),
            "agent product detail": lambda: c.get(f"/api/v3/agents/products/{some}", headers=k),
            "MCP search_products": lambda: c.post("/api/v3/mcp", headers=mcp_h, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_products", "arguments": {"query": "lighting"}}}),
            "intelligence overview": lambda: c.get("/api/v1/intelligence/overview", headers=h),
            "catalog quality list": lambda: c.get("/api/v1/intelligence/catalog-quality?page_size=50", headers=h),
            "system status": lambda: c.get("/api/v1/system/status", headers=h),
        }
        # one-off heavy operations (timed once each)
        t = time.perf_counter()
        r = await c.post("/api/v1/intelligence/refresh", headers=h)
        out["intelligence refresh (full catalog)"] = {"ms": round((time.perf_counter() - t) * 1000), "status": r.status_code}
        t = time.perf_counter()
        r = await c.post("/api/v1/intelligence/refresh", headers=h)
        out["intelligence refresh (nothing changed)"] = {"ms": round((time.perf_counter() - t) * 1000), "status": r.status_code}
        f = (await c.post("/api/v1/distribution/feeds", headers=h, json={"name": "Perf feed", "format": "json"})).json()
        t = time.perf_counter()
        await c.post(f"/api/v1/distribution/feeds/{f['id']}/generate", headers=h)
        await feeds.wait_for_inline()
        snaps = (await c.get(f"/api/v1/distribution/feeds/{f['id']}/snapshots", headers=h))
        out["feed generation (json, full)"] = {"ms": round((time.perf_counter() - t) * 1000), "status": snaps.status_code}
        t = time.perf_counter()
        r = await c.post("/api/v1/intelligence/reports", headers=h, json={"kind": "catalog_quality", "format": "pdf"})
        out["report generation (catalog quality PDF)"] = {"ms": round((time.perf_counter() - t) * 1000), "status": r.status_code}
        for name, fn in cases.items():
            await fn()  # warm-up
            out[name] = await _time(fn, args.runs)
    width = max(len(k) for k in out)
    for name, v in out.items():
        print(f"{name:<{width}}  {v}")
    res = ROOT / "eval" / "results"
    res.mkdir(exist_ok=True)
    (res / f"perf-{args.products}.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
