from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth, utc
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.ratelimit import clear_failures, failure_count, rate_limit, record_failure
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.models import Business, PasswordResetToken, RevokedToken, User
from app.schemas.api import (
    AccountUpdate,
    BusinessOut,
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    MeOut,
    RegisterIn,
    ResetPasswordIn,
    TokenOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("agentready.auth")
# A constant hash so login timing does not reveal whether an email exists.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password-1")


def _business_out(b: Business) -> BusinessOut:
    return BusinessOut(id=str(b.id), name=b.name, contact_email=b.contact_email, default_currency=b.default_currency,
                       country=b.country, catalog_api_key_prefix=b.catalog_api_key_prefix)


def me_out(ctx: AuthContext) -> MeOut:
    assert ctx.user is not None
    return MeOut(id=str(ctx.user.id), email=ctx.user.email, full_name=ctx.user.full_name, business=_business_out(ctx.business))


def _token(user: User) -> TokenOut:
    token, exp = create_access_token(user.id)
    return TokenOut(access_token=token, expires_at=exp.isoformat())


@router.post("/register", response_model=TokenOut, status_code=201,
             dependencies=[Depends(rate_limit("register"))])
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    email = body.email.lower()
    if (await db.execute(select(User.id).where(func.lower(User.email) == email))).first():
        raise AppError(409, "email_taken", "An account with this email already exists")
    user = User(email=email, password_hash=hash_password(body.password), full_name=body.full_name,
                password_changed_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    db.add(user)
    await db.flush()
    db.add(Business(owner_id=user.id, name=body.business_name, contact_email=email))
    await db.commit()
    return _token(user)


@router.post("/login", response_model=TokenOut, dependencies=[Depends(rate_limit("login"))])
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    s = get_settings()
    # Per-account throttle (any source IP). Counted for unknown emails too, so it reveals nothing.
    fail_key = "login:" + hash_opaque_token(body.email.lower())
    window = s.login_failure_window_seconds
    if await failure_count(fail_key, window) >= s.login_failures_per_account:
        raise AppError(429, "too_many_failed_logins",
                       "Too many failed sign-in attempts for this account. Try again later or reset your password.")
    user = (await db.execute(select(User).where(func.lower(User.email) == body.email.lower()))).scalar_one_or_none()
    if user is None:
        verify_password(body.password, _DUMMY_HASH)
        await record_failure(fail_key, window)
        raise AppError(401, "invalid_credentials", "Incorrect email or password")
    if not verify_password(body.password, user.password_hash) or not user.is_active:
        await record_failure(fail_key, window)
        raise AppError(401, "invalid_credentials", "Incorrect email or password")
    await clear_failures(fail_key)
    return _token(user)


@router.post("/logout", status_code=204)
async def logout(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    exp = datetime.fromtimestamp(ctx.claims["exp"], tz=timezone.utc)
    if not await db.get(RevokedToken, ctx.claims["jti"]):
        db.add(RevokedToken(jti=ctx.claims["jti"], expires_at=exp))
        await db.commit()
    return Response(status_code=204)


@router.get("/me", response_model=MeOut)
async def me(ctx: AuthContext = Depends(current_auth)):
    return me_out(ctx)


@router.patch("/me", response_model=MeOut)
async def update_me(body: AccountUpdate, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    assert ctx.user is not None
    if "full_name" in body.model_fields_set:
        ctx.user.full_name = body.full_name
    await db.commit()
    return me_out(ctx)


@router.post("/forgot-password", dependencies=[Depends(rate_limit("forgot", limit=5))])
async def forgot_password(body: ForgotPasswordIn, db: AsyncSession = Depends(get_db)):
    """Always returns the same response so it cannot be used to enumerate accounts.

    No email provider is configured in Phase 1: the reset link is written to the
    server log, and in development it is also returned as ``dev_reset_url``.
    """
    s = get_settings()
    user = (await db.execute(select(User).where(func.lower(User.email) == body.email.lower()))).scalar_one_or_none()
    response: dict = {"message": "If an account exists for that email, a reset link has been sent."}
    if user is not None:
        token = generate_opaque_token()
        db.add(PasswordResetToken(user_id=user.id, token_hash=hash_opaque_token(token),
                                  expires_at=datetime.now(timezone.utc) + timedelta(minutes=s.password_reset_expire_minutes)))
        await db.commit()
        url = f"{s.frontend_url.rstrip('/')}/reset-password?token={token}"
        if s.environment == "development":
            # No email provider in Phase 1. The link (a credential) is only ever exposed in development.
            log.info("Password reset requested for user %s (development). Reset URL: %s", user.id, url)
            response["dev_reset_url"] = url
        else:
            log.warning("Password reset requested for user %s but no email provider is configured; "
                        "the reset link was not delivered", user.id)
    return response


@router.post("/reset-password", dependencies=[Depends(rate_limit("reset", limit=10))])
async def reset_password(body: ResetPasswordIn, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_opaque_token(body.token))
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None or row.used_at is not None or utc(row.expires_at) < now:
        raise AppError(400, "invalid_token", "This reset link is invalid or has expired")
    user = await db.get(User, row.user_id)
    assert user is not None
    user.password_hash = hash_password(body.password)
    user.password_changed_at = now
    row.used_at = now
    # every other outstanding reset link for this account stops working too
    await db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                      PasswordResetToken.used_at.is_(None))
                     .values(used_at=now).execution_options(synchronize_session=False))
    await db.commit()
    await clear_failures("login:" + hash_opaque_token(user.email.lower()))
    return {"message": "Password updated. Please sign in."}


@router.post("/change-password", response_model=TokenOut)
async def change_password(body: ChangePasswordIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    assert ctx.user is not None
    if not verify_password(body.current_password, ctx.user.password_hash):
        raise AppError(400, "invalid_password", "Current password is incorrect")
    ctx.user.password_hash = hash_password(body.new_password)
    ctx.user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
    # all previous sessions are invalidated; hand back a fresh token (issued 1s later)
    token, exp = create_access_token(ctx.user.id)
    return TokenOut(access_token=token, expires_at=exp.isoformat())
