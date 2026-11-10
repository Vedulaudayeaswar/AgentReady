"""External commerce integrations. Credentials are encrypted at rest and never returned."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.core.config import get_settings
from app.core.errors import AppError, not_found
from app.core.ratelimit import rate_limit
from app.core.security import hash_opaque_token
from app.db.session import get_db
from app.models import CommerceIntegration, OAuthState, SyncJob
from app.schemas.commerce import IntegrationSyncIn, ShopifyConnectIn, WooConnectIn
from app.services.commerce.audit import actor_from_ctx, record
from app.services.commerce.money import aware, now_utc
from app.services.commerce.sync import sync_job_dict
from app.services.crawler.urls import InvalidURL, validate_public_url_syntax
from app.services.integrations.crypto import CredentialError, encrypt_json, hint
from app.services.integrations.http import SafeAPIClient
from app.services.integrations.providers import valid_shop_domain, verify_oauth_hmac
from app.services.integrations.service import (
    handle_webhook,
    integration_dict,
    new_webhook_secret,
    run_push,
    verify_integration,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _uuid(v, what):
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        raise not_found(what)


async def owned_integration(db: AsyncSession, ctx: AuthContext, integration_id: str) -> CommerceIntegration:
    i = (await db.execute(select(CommerceIntegration).where(
        CommerceIntegration.id == _uuid(integration_id, "Integration"),
        CommerceIntegration.business_id == ctx.business_id))).scalar_one_or_none()
    if i is None:
        raise not_found("Integration")
    return i


@router.get("")
async def list_integrations(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    rows = (await db.execute(select(CommerceIntegration).where(CommerceIntegration.business_id == ctx.business_id)
                             .order_by(CommerceIntegration.created_at))).scalars().all()
    return {"items": [integration_dict(i) for i in rows if i.status != "disconnected"],
            "providers": {
                "woocommerce": {"available": True, "auth": "REST API consumer key + secret (HTTPS)",
                                "docs": "https://woocommerce.github.io/woocommerce-rest-api-docs/"},
                "shopify": {"available": True, "oauth_configured": bool(s.shopify_api_key and s.shopify_api_secret),
                            "api_version": s.shopify_api_version,
                            "auth": "OAuth (needs SHOPIFY_API_KEY / SHOPIFY_API_SECRET) or a custom-app Admin API access token",
                            "docs": "https://shopify.dev/docs/apps/auth"},
            }}


async def _save(db: AsyncSession, ctx: AuthContext, provider: str, store_url: str, creds: dict, name: str | None,
                settings: dict, secret_hint: str) -> CommerceIntegration:
    try:
        encrypted = encrypt_json(creds)
    except CredentialError as e:
        raise AppError(503, "encryption_unavailable", str(e))
    i = (await db.execute(select(CommerceIntegration).where(
        CommerceIntegration.business_id == ctx.business_id, CommerceIntegration.provider == provider,
        CommerceIntegration.store_url == store_url))).scalar_one_or_none()
    if i is None:
        i = CommerceIntegration(business_id=ctx.business_id, provider=provider, store_url=store_url, settings={})
        db.add(i)
    i.name, i.credentials_encrypted, i.credentials_hint = name or store_url, encrypted, secret_hint
    i.settings, i.status, i.last_error = {**(i.settings or {}), **settings}, "pending", None
    await db.flush()
    return i


@router.post("/woocommerce/connect", status_code=201, dependencies=[Depends(rate_limit("integration_connect", limit=10))])
async def connect_woocommerce(body: WooConnectIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    try:
        url = validate_public_url_syntax(body.store_url).rstrip("/")
    except InvalidURL as e:
        raise AppError(422, "invalid_url", str(e))
    if not url.startswith("https://") and not get_settings().allow_private_networks:
        raise AppError(422, "https_required", "Use the store's https:// address (API keys are sent with every request)")
    i = await _save(db, ctx, "woocommerce", url, {"consumer_key": body.consumer_key, "consumer_secret": body.consumer_secret},
                    body.name, {"inventory_master": body.inventory_master}, hint(body.consumer_key))
    secret = new_webhook_secret(i)
    result = await verify_integration(db, i, actor_from_ctx(ctx))
    record(db, ctx.business_id, actor_from_ctx(ctx), "integration.connected", "integration", i.id,
           f"WooCommerce store {url} connected ({'verified' if result['ok'] else 'verification failed'})",
           {"credentials_hint": i.credentials_hint})
    await db.commit()
    return {**integration_dict(i), "verification": result, "webhook_secret": secret,
            "webhook_setup": "In WooCommerce → Settings → Advanced → Webhooks, add a 'Product updated' webhook with "
                             f"delivery URL {get_settings().api_base_url}/api/v1/integrations/{i.id}/webhooks and this secret. "
                             "The secret is shown once."}


@router.post("/shopify/connect", status_code=201, dependencies=[Depends(rate_limit("integration_connect", limit=10))])
async def connect_shopify(body: ShopifyConnectIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    shop = body.shop.strip().lower().removeprefix("https://").rstrip("/")
    if not valid_shop_domain(shop):
        raise AppError(422, "invalid_shop", "Enter the store's myshopify.com domain, e.g. my-store.myshopify.com")
    if body.access_token:
        i = await _save(db, ctx, "shopify", f"https://{shop}", {"access_token": body.access_token}, body.name,
                        {"inventory_master": body.inventory_master, "auth": "access_token"}, hint(body.access_token))
        result = await verify_integration(db, i, actor_from_ctx(ctx))
        record(db, ctx.business_id, actor_from_ctx(ctx), "integration.connected", "integration", i.id,
               f"Shopify store {shop} connected with an Admin API token ({'verified' if result['ok'] else 'verification failed'})")
        await db.commit()
        return {**integration_dict(i), "verification": result}
    if not (s.shopify_api_key and s.shopify_api_secret):
        raise AppError(503, "shopify_not_configured",
                       "Shopify OAuth needs SHOPIFY_API_KEY and SHOPIFY_API_SECRET from a Shopify app. Ask your administrator, "
                       "or connect with a custom-app Admin API access token.")
    state = secrets.token_urlsafe(32)
    db.add(OAuthState(business_id=ctx.business_id, provider="shopify", state_hash=hash_opaque_token(state), shop=shop,
                      expires_at=now_utc() + timedelta(minutes=10)))
    await db.commit()
    redirect = f"{s.api_base_url}/api/v1/integrations/shopify/callback"
    url = f"https://{shop}/admin/oauth/authorize?" + urlencode(
        {"client_id": s.shopify_api_key, "scope": s.shopify_scopes, "redirect_uri": redirect, "state": state})
    return {"authorize_url": url, "status": "redirect_required"}


@router.get("/shopify/callback")
async def shopify_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """OAuth redirect target. Verifies Shopify's HMAC, the single-use state and the shop domain."""
    s = get_settings()
    params = dict(request.query_params)
    shop = params.get("shop", "").lower()
    if not (s.shopify_api_secret and verify_oauth_hmac(params, s.shopify_api_secret)) or not valid_shop_domain(shop):
        return JSONResponse(status_code=400, content={"error": {"code": "invalid_callback", "message": "Invalid OAuth callback"}})
    st = (await db.execute(select(OAuthState).where(OAuthState.state_hash == hash_opaque_token(params.get("state", ""))))).scalar_one_or_none()
    if st is None or st.used_at is not None or aware(st.expires_at) < now_utc() or st.shop != shop:
        return JSONResponse(status_code=400, content={"error": {"code": "invalid_state", "message": "OAuth state expired or invalid"}})
    st.used_at = now_utc()
    client = SafeAPIClient()
    try:
        resp = await client.request("POST", f"https://{shop}/admin/oauth/access_token",
                                    json_body={"client_id": s.shopify_api_key, "client_secret": s.shopify_api_secret,
                                               "code": params.get("code", "")})
    finally:
        await client.aclose()
    token = (resp.json() or {}).get("access_token") if resp.status == 200 else None
    if not token:
        await db.commit()
        return JSONResponse(status_code=400, content={"error": {"code": "token_exchange_failed", "message": "Shopify token exchange failed"}})
    i = (await db.execute(select(CommerceIntegration).where(
        CommerceIntegration.business_id == st.business_id, CommerceIntegration.provider == "shopify",
        CommerceIntegration.store_url == f"https://{shop}"))).scalar_one_or_none()
    if i is None:
        i = CommerceIntegration(business_id=st.business_id, provider="shopify", store_url=f"https://{shop}", name=shop, settings={})
        db.add(i)
    i.credentials_encrypted, i.credentials_hint = encrypt_json({"access_token": token}), hint(token)
    i.settings = {**(i.settings or {}), "auth": "oauth"}
    from app.services.commerce.audit import Actor

    await verify_integration(db, i, Actor.system("Shopify OAuth"))
    await db.commit()
    return RedirectResponse(f"{s.frontend_url}/app/integrations?connected=shopify", status_code=302)


@router.post("/{integration_id}/verify")
async def verify(integration_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    i = await owned_integration(db, ctx, integration_id)
    result = await verify_integration(db, i, actor_from_ctx(ctx))
    await db.commit()
    return {**integration_dict(i), "verification": result}


async def start_integration_sync(db: AsyncSession, ctx: AuthContext, i: CommerceIntegration, direction: str,
                                 product_ids: list[uuid.UUID] | None = None, push_prices: bool = True,
                                 push_inventory: bool = True) -> dict:
    if i.status == "disconnected":
        raise AppError(409, "disconnected", "This integration is disconnected")
    running = (await db.execute(select(SyncJob.id).where(SyncJob.integration_id == i.id,
                                                         SyncJob.status.in_(("queued", "running"))))).first()
    if running:
        raise AppError(409, "sync_in_progress", "A sync is already running for this store")
    actor = actor_from_ctx(ctx)
    if direction == "export":
        job = await run_push(db, i, actor, product_ids, push_prices, push_inventory)
        return sync_job_dict(job)
    from app.workers.integration_jobs import dispatch_import

    job = SyncJob(business_id=i.business_id, source_type=i.provider, integration_id=i.id, source_label=i.store_url,
                  direction="import", trigger="manual", status="queued", created_by=actor.id, errors=[], summary={})
    db.add(job)
    record(db, ctx.business_id, actor, "sync.started", "integration", i.id, f"{i.provider.title()} import started")
    await db.commit()
    try:
        backend = dispatch_import(job.id)
    except Exception:
        job.status, job.errors, job.finished_at = "failed", [{"message": "Background worker unavailable"}], now_utc()
        await db.commit()
        raise AppError(503, "worker_unavailable", "The sync could not be started: the background worker is unavailable.")
    return {**sync_job_dict(job), "job_backend": backend}


@router.post("/{integration_id}/sync", status_code=202, dependencies=[Depends(rate_limit("integration_sync", limit=10))])
async def sync(integration_id: str, body: IntegrationSyncIn, ctx: AuthContext = Depends(current_auth),
               db: AsyncSession = Depends(get_db)):
    i = await owned_integration(db, ctx, integration_id)
    ids = [_uuid(x, "Product") for x in body.product_ids] if body.product_ids else None
    return await start_integration_sync(db, ctx, i, body.direction, ids, body.push_prices, body.push_inventory)


@router.post("/{integration_id}/webhook-secret", status_code=201)
async def rotate_webhook_secret(integration_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    i = await owned_integration(db, ctx, integration_id)
    if i.provider != "woocommerce":
        raise AppError(422, "not_supported", "Shopify webhooks are signed with the app's client secret")
    secret = new_webhook_secret(i)
    record(db, ctx.business_id, actor_from_ctx(ctx), "integration.webhook_secret_rotated", "integration", i.id,
           "Webhook secret rotated")
    await db.commit()
    return {"webhook_secret": secret}


@router.delete("/{integration_id}", status_code=200)
async def disconnect(integration_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Disconnect: credentials are erased. Imported products stay in the catalog."""
    i = await owned_integration(db, ctx, integration_id)
    i.status, i.credentials_encrypted, i.webhook_secret_encrypted = "disconnected", None, None
    record(db, ctx.business_id, actor_from_ctx(ctx), "integration.disconnected", "integration", i.id,
           f"{i.provider.title()} store {i.store_url} disconnected (credentials erased)")
    await db.commit()
    return {"id": str(i.id), "status": i.status}


@router.post("/{integration_id}/webhooks", include_in_schema=True)
async def webhook(integration_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Store webhooks (no session: authenticated by the provider's HMAC signature)."""
    try:
        iid = uuid.UUID(integration_id)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Not found"}})
    i = await db.get(CommerceIntegration, iid)
    if i is None or i.status == "disconnected":
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Not found"}})
    raw = await request.body()
    if len(raw) > 2_000_000:
        return JSONResponse(status_code=413, content={"error": {"code": "too_large", "message": "Payload too large"}})
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        status, detail = await handle_webhook(db, i, headers, raw)
    except CredentialError:
        return JSONResponse(status_code=401, content={"error": {"code": "invalid_signature", "message": "invalid signature"}})
    if status >= 400:
        return JSONResponse(status_code=status, content={"error": {"code": "webhook_rejected", "message": detail}})
    return {"ok": True, "detail": detail}
