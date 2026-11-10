from app.core import ratelimit
from app.tests.conftest import register

API = "/api/v1/auth"


async def test_register_login_me_logout(client):
    h = await register(client)
    me = (await client.get(f"{API}/me", headers=h)).json()
    assert me["email"] == "owner@example.com" and me["business"]["name"] == "Sharma Electricals"
    assert (await client.post(f"{API}/register", json={"email": "OWNER@example.com", "password": "another-pass-9",
                                                      "business_name": "x"})).status_code == 409
    r = await client.post(f"{API}/login", json={"email": "owner@example.com", "password": "correct-horse-42"})
    assert r.status_code == 200
    token = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.post(f"{API}/logout", headers=token)).status_code == 204
    assert (await client.get(f"{API}/me", headers=token)).status_code == 401  # revoked


async def test_bad_credentials_and_validation(client):
    await register(client)
    r = await client.post(f"{API}/login", json={"email": "owner@example.com", "password": "wrong-password-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await client.post(f"{API}/login", json={"email": "nobody@example.com", "password": "wrong-password-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"  # same response
    r = await client.post(f"{API}/register", json={"email": "x@example.com", "password": "short", "business_name": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert (await client.get(f"{API}/me")).status_code == 401
    assert (await client.get(f"{API}/me", headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_password_hash_is_not_plaintext(client, db):
    from sqlalchemy import select

    from app.models import User

    await register(client)
    user = (await db.execute(select(User))).scalar_one()
    assert "correct-horse-42" not in user.password_hash and user.password_hash.startswith("$2")


async def test_forgot_and_reset_password(client):
    old = await register(client)
    r = await client.post(f"{API}/forgot-password", json={"email": "nobody@example.com"})
    assert "dev_reset_url" not in r.json()  # no enumeration
    # dev-only convenience is disabled outside ENVIRONMENT=development
    from app.core.config import get_settings

    get_settings().environment = "development"
    r = await client.post(f"{API}/forgot-password", json={"email": "owner@example.com"})
    token = r.json()["dev_reset_url"].split("token=")[1]
    assert (await client.post(f"{API}/reset-password", json={"token": token, "password": "brand-new-pass-7"})).status_code == 200
    assert (await client.post(f"{API}/reset-password", json={"token": token, "password": "brand-new-pass-8"})).status_code == 400
    assert (await client.get(f"{API}/me", headers=old)).status_code == 401  # old sessions invalidated
    assert (await client.post(f"{API}/login", json={"email": "owner@example.com", "password": "brand-new-pass-7"})).status_code == 200


async def test_login_rate_limit(client, settings):
    settings.auth_rate_limit_per_minute = 3
    ratelimit.reset_rate_limits()
    codes = [(await client.post(f"{API}/login", json={"email": "a@example.com", "password": "x"})).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
