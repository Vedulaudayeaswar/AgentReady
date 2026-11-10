from __future__ import annotations

import uuid
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, hash_opaque_token
from app.db.session import get_db
from app.models import Business, RevokedToken, User

bearer = HTTPBearer(auto_error=False)


class AuthContext:
    def __init__(self, user: User | None, business: Business, claims: dict | None = None):
        self.user = user
        self.business = business
        self.claims = claims or {}

    @property
    def business_id(self) -> uuid.UUID:
        return self.business.id


async def _user_from_token(token: str, db: AsyncSession) -> tuple[User, dict]:
    try:
        claims = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired", headers={"WWW-Authenticate": "Bearer"})
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    if await db.get(RevokedToken, claims["jti"]):
        raise HTTPException(401, "Token revoked", headers={"WWW-Authenticate": "Bearer"})
    try:
        user = await db.get(User, uuid.UUID(claims["sub"]))
    except ValueError:
        user = None
    if user is None or not user.is_active:
        raise HTTPException(401, "User not found or inactive")
    changed = user.password_changed_at
    if changed is not None:
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        if int(claims["iat_ms"]) < int(changed.timestamp() * 1000):
            raise HTTPException(401, "Session invalidated by password change")
    return user, claims


async def current_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)
) -> AuthContext:
    if creds is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    user, claims = await _user_from_token(creds.credentials, db)
    business = (await db.execute(select(Business).where(Business.owner_id == user.id))).scalar_one_or_none()
    if business is None:
        raise HTTPException(403, "No business profile")
    return AuthContext(user, business, claims)


ALL_SCOPES = ("catalog:read", "pricing:read", "inventory:read", "evidence:read", "agent:read", "checkout:write")


async def verify_api_key(db: AsyncSession, raw: str):
    """Validate a scoped API key, enforce its rate limit and count the request. Returns (ApiKey, Business).

    Keys are stored as SHA-256 hashes; revoked/expired keys are refused; each key has its own rate
    limit and usage counters (last used, total, per day).
    """
    from sqlalchemy import update as sa_update
    from sqlalchemy.exc import IntegrityError

    from app.core.ratelimit import hit
    from app.models import ApiKey, ApiKeyUsage

    key = (await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_opaque_token(raw)))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if key is None or key.revoked_at is not None or (key.expires_at is not None and utc(key.expires_at) <= now):
        raise HTTPException(401, "Invalid or revoked API key")
    if not await hit(f"apikey:{key.id}", key.rate_limit_per_minute or 120):
        raise HTTPException(429, "API key rate limit exceeded", headers={"Retry-After": "60"})
    business = await db.get(Business, key.business_id)
    if business is None:
        raise HTTPException(401, "Invalid or revoked API key")
    await db.execute(sa_update(ApiKey).where(ApiKey.id == key.id).values(
        last_used_at=now, usage_count=ApiKey.usage_count + 1).execution_options(synchronize_session=False))
    day = now.date().isoformat()
    res = await db.execute(sa_update(ApiKeyUsage).where(ApiKeyUsage.api_key_id == key.id, ApiKeyUsage.day == day)
                           .values(requests=ApiKeyUsage.requests + 1).execution_options(synchronize_session=False))
    if res.rowcount == 0:
        try:
            async with db.begin_nested():
                db.add(ApiKeyUsage(api_key_id=key.id, business_id=key.business_id, day=day, requests=1))
        except IntegrityError:
            pass
    await db.commit()
    return key, business


async def catalog_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_catalog_key: str | None = Header(default=None, alias="X-Catalog-Key"),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Merchant session token OR a scoped catalog API key (read-only, published catalog only)."""
    if x_catalog_key:
        try:
            key, business = await verify_api_key(db, x_catalog_key)
        except HTTPException as e:
            if e.status_code == 401:
                raise HTTPException(401, "Invalid or revoked catalog API key")
            raise
        return AuthContext(None, business, {"via": "api_key", "api_key_id": str(key.id), "scopes": list(key.scopes or [])})
    ctx = await current_auth(creds, db)
    ctx.claims = {**ctx.claims, "scopes": list(ALL_SCOPES)}
    return ctx


def has_scope(ctx: AuthContext, scope: str) -> bool:
    return scope in (ctx.claims or {}).get("scopes", ALL_SCOPES)


def utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
