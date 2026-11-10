import os

os.environ.update({
    "ENVIRONMENT": "test",
    "SECRET_KEY": "test-secret-key-0123456789abcdef0123456789",
    "AI_PROVIDER": "none",
    "GROQ_API_KEY": "",
    "JOB_BACKEND": "inline",
    "CRAWL_DELAY_SECONDS": "0",
    "CRAWL_MAX_RETRIES": "0",
    "AUTH_RATE_LIMIT_PER_MINUTE": "1000",
    "LOG_LEVEL": "WARNING",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.core import ratelimit  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.services.crawler import fetcher as fetcher_mod  # noqa: E402
from app.tests.sitekit import FixtureSite, public_resolver  # noqa: E402

import app.models  # noqa: E402,F401


@pytest.fixture
def settings():
    s = get_settings()
    snapshot = s.model_dump()
    yield s
    for k, v in snapshot.items():
        setattr(s, k, v)


@pytest.fixture
async def db_engine(tmp_path):
    # TEST_DATABASE_URL=postgresql+asyncpg://... runs the whole suite against PostgreSQL
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = db_session.set_engine(url)
    async with engine.begin() as conn:
        if url.startswith("postgresql"):
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(db_engine):
    async with db_session.session_factory()() as s:
        yield s


@pytest.fixture
def site(monkeypatch):
    site = FixtureSite()
    monkeypatch.setattr(fetcher_mod, "TRANSPORT_OVERRIDE", site.transport())
    monkeypatch.setattr(fetcher_mod, "RESOLVER_OVERRIDE", public_resolver)
    return site


@pytest.fixture
async def client(db_engine, settings):
    ratelimit.reset_rate_limits()
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register(client: httpx.AsyncClient, email: str = "owner@example.com", business: str = "Sharma Electricals") -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "correct-horse-42", "full_name": "Test Owner", "business_name": business})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
