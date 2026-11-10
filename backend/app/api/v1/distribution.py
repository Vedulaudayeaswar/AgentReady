"""Merchant Distribution control center (dashboard API). Every query is scoped to the merchant's business."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.products import contains_pattern
from app.core.config import get_settings
from app.core.errors import AppError, not_found
from app.db.session import get_db
from app.models import (ApiKey, ApiKeyUsage, Category, CheckoutSession, DistributionEvent, FeedConfiguration,
                        FeedSnapshot, FeedToken, Product, WebhookDelivery, WebhookSubscription, live_products)
from app.schemas.distribution import (BulkDistributionIn, CategoryDistributionIn, DistributionSettingsIn, FeedIn,
                                      FeedTokenIn, FeedUpdateIn, PlatformApplicationIn, PlatformIn,
                                      ProductDistributionIn, WebhookIn, WebhookUpdateIn)
from app.services.catalog.serialize import is_public
from app.services.commerce.audit import actor_from_ctx, record
from app.services.commerce.money import aware, now_utc
from app.services.distribution import analytics, feeds, platforms, webhooks
from app.services.distribution.checkout import session_dict
from app.services.distribution.profile import channel_open, get_profile, merchant_scope, profile_dict
from app.services.distribution.public import distribution_status, eligibility_clause, public_product

router = APIRouter(prefix="/distribution", tags=["distribution"])


def _uuid(v: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise not_found(what)


async def _scope(db, ctx: AuthContext):
    prof = await get_profile(db, ctx.business)
    return prof, await merchant_scope(db, ctx.business, prof)


async def _product(db, ctx, product_id) -> Product:
    p = (await db.execute(select(Product).where(Product.id == _uuid(product_id, "Product"),
                                                *live_products(ctx.business_id)))).scalar_one_or_none()
    if p is None:
        raise not_found("Product")
    return p


# ================================================================ settings & overview
@router.get("/settings")
async def get_settings_(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prof = await get_profile(db, ctx.business)
    await db.commit()
    return profile_dict(prof)


@router.put("/settings")
async def update_settings(body: DistributionSettingsIn, ctx: AuthContext = Depends(current_auth),
                          db: AsyncSession = Depends(get_db)):
    prof = await get_profile(db, ctx.business)
    data = body.model_dump(exclude_unset=True)
    changes = {}
    if data.pop("reset_public_attribute_keys", False):
        prof.public_attribute_keys = None
        changes["public_attribute_keys"] = None
    if data.pop("clear_expiry", False):
        prof.expires_at = None
        changes["expires_at"] = None
    if "slug" in data and data["slug"] != prof.slug:
        from app.models import DistributionProfile

        taken = (await db.execute(select(DistributionProfile.id).where(DistributionProfile.slug == data["slug"],
                                                                        DistributionProfile.id != prof.id))).first()
        if taken:
            raise AppError(409, "slug_taken", "This public merchant id is already used by another merchant")
    if "excluded_category_ids" in data and data["excluded_category_ids"] is not None:
        ids = [_uuid(c, "Category") for c in data["excluded_category_ids"]]
        owned = set((await db.execute(select(Category.id).where(Category.business_id == ctx.business_id,
                                                                 Category.id.in_(ids)))).scalars().all())
        if len(owned) != len(set(ids)):
            raise not_found("Category")
        data["excluded_category_ids"] = [str(i) for i in ids]
    for k in ("public_attribute_keys", "hidden_attribute_keys"):
        if data.get(k) is not None:
            data[k] = sorted({str(x).strip().lower() for x in data[k] if str(x).strip()})
    for k in ("public_url", "return_policy_url", "privacy_policy_url", "terms_url"):
        if data.get(k) is not None:
            data[k] = str(data[k])
    for k, v in data.items():
        if getattr(prof, k) != v:
            changes[k] = v
            setattr(prof, k, v)
    if changes:
        record(db, ctx.business_id, actor_from_ctx(ctx), "distribution.settings_updated", "distribution", prof.id,
               "Distribution settings updated: " + ", ".join(sorted(changes)), changes)
    await db.commit()
    await db.refresh(prof)
    return profile_dict(prof)


@router.get("/overview")
async def overview(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prof, scope = await _scope(db, ctx)
    await db.commit()
    bid = ctx.business_id
    distributed = (await db.execute(select(func.count()).select_from(Product).where(eligibility_clause(scope, "agent_api")))).scalar_one()
    approved_unpublished = (await db.execute(select(func.count()).select_from(Product).where(
        *live_products(bid), Product.approval_status == "approved", Product.publication_status != "published"))).scalar_one()
    awaiting_review = (await db.execute(select(func.count()).select_from(Product).where(
        *live_products(bid), Product.approval_status != "approved"))).scalar_one()
    published = (await db.execute(select(func.count()).select_from(Product).where(
        *live_products(bid), Product.approval_status == "approved", Product.publication_status == "published"))).scalar_one()
    active_feeds = (await db.execute(select(func.count()).select_from(FeedConfiguration).where(
        FeedConfiguration.business_id == bid, FeedConfiguration.archived_at.is_(None),
        FeedConfiguration.latest_snapshot_id.is_not(None)))).scalar_one()
    feeds_total = (await db.execute(select(func.count()).select_from(FeedConfiguration).where(
        FeedConfiguration.business_id == bid, FeedConfiguration.archived_at.is_(None)))).scalar_one()
    active_tokens = sum(1 for t in (await db.execute(select(FeedToken).where(FeedToken.business_id == bid))).scalars() if feeds.token_active(t))
    since = analytics.since_days(7)
    by_kind = dict((await db.execute(select(DistributionEvent.kind, func.count()).where(
        DistributionEvent.business_id == bid, DistributionEvent.created_at >= since).group_by(DistributionEvent.kind))).all())
    subs = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.business_id == bid))).scalars().all()
    wh = dict((await db.execute(select(WebhookDelivery.status, func.count()).where(
        WebhookDelivery.business_id == bid, WebhookDelivery.created_at >= since).group_by(WebhookDelivery.status))).all())
    failed_feeds = (await db.execute(select(func.count()).select_from(FeedSnapshot).where(
        FeedSnapshot.business_id == bid, FeedSnapshot.status == "failed", FeedSnapshot.created_at >= since))).scalar_one()
    errors = by_kind.get("error", 0)
    requests = sum(n for k, n in by_kind.items() if k in analytics.REQUEST_KINDS or k == "mcp_call")
    channels = {c: channel_open(prof, c)[0] for c in ("agent_api", "mcp", "feeds", "webhooks")}
    checklist = [
        {"key": "approved", "label": "Approve and publish products", "done": published > 0},
        {"key": "enabled", "label": "Turn on public distribution", "done": prof.enabled},
        {"key": "identity", "label": "Set your public store name and website", "done": bool(prof.public_name and prof.public_url)},
        {"key": "feed", "label": "Generate a product feed", "done": active_feeds > 0},
        {"key": "key", "label": "Create an agent API key", "done": (await db.execute(select(ApiKey.id).where(
            ApiKey.business_id == bid, ApiKey.revoked_at.is_(None)))).first() is not None},
    ]
    return {"settings": profile_dict(prof), "channels": channels, "checklist": checklist,
            "products": {"distributed": distributed, "published": published, "approved_unpublished": approved_unpublished,
                         "awaiting_review": awaiting_review},
            "feeds": {"active": active_feeds, "total": feeds_total, "active_tokens": active_tokens, "failed_7d": failed_feeds},
            "last_7_days": {"agent_requests": requests, "mcp_tool_calls": by_kind.get("mcp_call", 0),
                            "feed_downloads": by_kind.get("feed_download", 0), "errors": errors,
                            "checkout_sessions": by_kind.get("checkout_session", 0)},
            "webhooks": {"subscriptions": len(subs), "active": sum(1 for s in subs if s.is_active),
                         "succeeded_7d": wh.get("succeeded", 0), "failed_7d": wh.get("failed", 0),
                         "pending": wh.get("pending", 0) + wh.get("delivering", 0)},
            "failed_operations_7d": failed_feeds + wh.get("failed", 0) + errors}


# ================================================================ product-level controls
@router.get("/products")
async def products(q: str | None = None, status: str | None = Query(None, pattern="^(distributed|not_distributed|eligible)$"),
                   page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prof, scope = await _scope(db, ctx)
    await db.commit()
    stmt = select(Product).where(*live_products(ctx.business_id))
    if q:
        like = contains_pattern(q)
        stmt = stmt.where(or_(func.lower(Product.name).like(like, escape="\\"), func.lower(Product.sku).like(like, escape="\\")))
    if status == "distributed":
        stmt = stmt.where(eligibility_clause(scope, "agent_api"))
    elif status == "not_distributed":
        stmt = stmt.where(~eligibility_clause(scope, "agent_api"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Product.name, Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = []
    for p in rows:
        st = distribution_status(p, scope)
        items.append({"id": str(p.id), "name": p.name, "sku": p.sku, "category": p.category,
                      "category_id": str(p.category_id) if p.category_id else None,
                      "approval_status": p.approval_status, "publication_status": p.publication_status,
                      "lifecycle_status": p.lifecycle_status, "is_public": is_public(p),
                      "distribution_enabled": p.distribution_enabled, "distribution_channels": p.distribution_channels,
                      "has_public_description": bool(p.public_description), "has_public_images": p.public_images is not None,
                      "public_attribute_keys": p.public_attribute_keys, "status": st})
    cats = (await db.execute(select(Category).where(Category.business_id == ctx.business_id, Category.archived_at.is_(None))
                             .order_by(Category.path_key))).scalars().all()
    excluded = {str(c) for c in prof.excluded_category_ids or []}
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "categories": [{"id": str(c.id), "path": c.path, "distributed": str(c.id) not in excluded and c.id not in scope.excluded_categories}
                           for c in cats]}


@router.patch("/products/{product_id}")
async def update_product(product_id: str, body: ProductDistributionIn, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    p = await _product(db, ctx, product_id)
    d = body.model_dump(exclude_unset=True)
    changes: dict = {}

    def setv(attr, value):
        if getattr(p, attr) != value:
            changes[attr] = value
            setattr(p, attr, value)

    if d.get("inherit"):
        setv("distribution_enabled", None)
    elif "distribution_enabled" in d:
        setv("distribution_enabled", d["distribution_enabled"])
    if d.get("all_channels"):
        setv("distribution_channels", None)
    elif d.get("distribution_channels") is not None:
        setv("distribution_channels", sorted(set(d["distribution_channels"])))
    if d.get("clear_public_description"):
        setv("public_description", None)
    elif d.get("public_description") is not None:
        setv("public_description", d["public_description"].strip() or None)
    if d.get("clear_public_images"):
        setv("public_images", None)
    elif d.get("public_images") is not None:
        setv("public_images", [str(u) for u in d["public_images"]])
    if d.get("clear_public_attribute_keys"):
        setv("public_attribute_keys", None)
    elif d.get("public_attribute_keys") is not None:
        setv("public_attribute_keys", sorted({k.strip().lower() for k in d["public_attribute_keys"] if k.strip()}))
    if changes:
        p.version += 1
        record(db, ctx.business_id, actor_from_ctx(ctx), "distribution.product_updated", "product", p.id,
               f"Distribution settings of '{p.name}' updated: {', '.join(sorted(changes))}", changes)
    await db.commit()
    prof, scope = await _scope(db, ctx)
    return {"id": str(p.id), "distribution_enabled": p.distribution_enabled, "distribution_channels": p.distribution_channels,
            "public_description": p.public_description, "public_images": p.public_images,
            "public_attribute_keys": p.public_attribute_keys, "status": distribution_status(p, scope)}


@router.post("/products/bulk")
async def bulk_products(body: BulkDistributionIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    ids = [_uuid(i, "Product") for i in body.product_ids]
    rows = (await db.execute(select(Product).where(Product.id.in_(ids), *live_products(ctx.business_id)))).scalars().all()
    if len(rows) != len(set(ids)):
        raise not_found("Product")
    value = None if body.inherit else body.distribution_enabled
    if value is None and not body.inherit:
        raise AppError(422, "validation_error", "Set distribution_enabled or inherit")
    changed = 0
    for p in rows:
        if p.distribution_enabled != value:
            p.distribution_enabled = value
            p.version += 1
            changed += 1
    record(db, ctx.business_id, actor_from_ctx(ctx), "distribution.products_bulk_updated", "product", None,
           f"Distribution {'inherited' if body.inherit else ('enabled' if value else 'disabled')} for {changed} products",
           {"count": changed, "distribution_enabled": value})
    await db.commit()
    return {"updated": changed, "total": len(rows)}


@router.put("/categories/{category_id}")
async def category_distribution(category_id: str, body: CategoryDistributionIn, ctx: AuthContext = Depends(current_auth),
                                db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(Category).where(Category.id == _uuid(category_id, "Category"),
                                                 Category.business_id == ctx.business_id))).scalar_one_or_none()
    if c is None:
        raise not_found("Category")
    prof = await get_profile(db, ctx.business)
    ex = [x for x in prof.excluded_category_ids or [] if x != str(c.id)]
    if not body.distributed:
        ex.append(str(c.id))
    prof.excluded_category_ids = ex
    record(db, ctx.business_id, actor_from_ctx(ctx), "distribution.category_updated", "category", c.id,
           f"Category '{c.path}' {'included in' if body.distributed else 'excluded from'} distribution")
    await db.commit()
    return profile_dict(prof)


@router.get("/products/{product_id}/preview")
async def preview(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Exactly what an external agent receives — produced by the same public serializer as the agent API."""
    p = await _product(db, ctx, product_id)
    prof, scope = await _scope(db, ctx)
    await db.commit()
    st = distribution_status(p, scope)
    return {"status": st, "representation": public_product(p, scope),
            "visible_to_agents": st["distributed"],
            "controls": {"distribution_enabled": p.distribution_enabled, "distribution_channels": p.distribution_channels,
                         "public_description": p.public_description, "public_images": p.public_images,
                         "public_attribute_keys": p.public_attribute_keys},
            "note": "Generated by the public serializer used by the agent API, MCP tools, feeds and webhooks."
                    + ("" if st["distributed"] else " This product is not visible to agents yet: see status.reasons.")}


# ================================================================ feeds
async def _feed(db, ctx, feed_id) -> FeedConfiguration:
    f = (await db.execute(select(FeedConfiguration).where(FeedConfiguration.id == _uuid(feed_id, "Feed"),
                                                          FeedConfiguration.business_id == ctx.business_id))).scalar_one_or_none()
    if f is None:
        raise not_found("Feed")
    return f


@router.get("/feeds")
async def list_feeds(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(FeedConfiguration).where(FeedConfiguration.business_id == ctx.business_id)
                             .order_by(FeedConfiguration.archived_at.is_not(None), FeedConfiguration.created_at.desc()))).scalars().all()
    out = []
    for f in rows:
        latest = (await db.execute(select(FeedSnapshot).where(FeedSnapshot.feed_id == f.id)
                                   .order_by(FeedSnapshot.version.desc()).limit(1))).scalar_one_or_none()
        toks = (await db.execute(select(FeedToken).where(FeedToken.feed_id == f.id).order_by(FeedToken.created_at.desc()))).scalars().all()
        d = feeds.feed_dict(f, latest, toks)
        if f.latest_snapshot_id and (latest is None or latest.id != f.latest_snapshot_id):
            served = await db.get(FeedSnapshot, f.latest_snapshot_id)
            d["served"] = feeds.snapshot_dict(served) if served else None
        else:
            d["served"] = d["latest"] if latest and latest.status == "completed" else None
        out.append(d)
    return {"items": out, "profiles": {k: list(v) for k, v in feeds.PROFILES.items()},
            "profile_types": {k: list(v) for k, v in feeds.PROFILE_TYPES.items()}, "schedules": list(feeds.SCHEDULES)}


@router.post("/feeds", status_code=201)
async def create_feed(body: FeedIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    cat = None
    if body.category_id:
        cat = (await db.execute(select(Category).where(Category.id == _uuid(body.category_id, "Category"),
                                                       Category.business_id == ctx.business_id))).scalar_one_or_none()
        if cat is None:
            raise not_found("Category")
    try:
        feeds.validate_config(body.profile, body.format, body.feed_type, cat.id if cat else None)
    except feeds.FeedError as e:
        raise AppError(422, "invalid_feed", str(e))
    f = FeedConfiguration(business_id=ctx.business_id, name=body.name, profile=body.profile, format=body.format,
                          feed_type=body.feed_type, category_id=cat.id if cat else None, schedule=body.schedule,
                          max_age_hours=body.max_age_hours, next_run_at=feeds.next_run(body.schedule))
    db.add(f)
    await db.flush()
    record(db, ctx.business_id, actor_from_ctx(ctx), "feed.created", "feed", f.id,
           f"Feed '{f.name}' created ({f.profile} {f.format}, {f.feed_type}, {f.schedule})")
    await db.commit()
    return feeds.feed_dict(f)


@router.patch("/feeds/{feed_id}")
async def update_feed(feed_id: str, body: FeedUpdateIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    f = await _feed(db, ctx, feed_id)
    d = body.model_dump(exclude_unset=True)
    for k, v in d.items():
        if v is not None:
            setattr(f, k, v)
    if "schedule" in d:
        f.next_run_at = feeds.next_run(f.schedule)
    record(db, ctx.business_id, actor_from_ctx(ctx), "feed.updated", "feed", f.id, f"Feed '{f.name}' updated", d)
    await db.commit()
    return feeds.feed_dict(f)


@router.delete("/feeds/{feed_id}")
async def archive_feed(feed_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    f = await _feed(db, ctx, feed_id)
    f.archived_at, f.next_run_at = now_utc(), None
    for t in (await db.execute(select(FeedToken).where(FeedToken.feed_id == f.id, FeedToken.revoked_at.is_(None)))).scalars():
        t.revoked_at = now_utc()
    record(db, ctx.business_id, actor_from_ctx(ctx), "feed.archived", "feed", f.id, f"Feed '{f.name}' archived; its URLs were revoked")
    await db.commit()
    return feeds.feed_dict(f)


@router.post("/feeds/{feed_id}/generate", status_code=202)
async def generate(feed_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    f = await _feed(db, ctx, feed_id)
    if f.archived_at is not None:
        raise AppError(409, "archived", "This feed is archived")
    running = (await db.execute(select(FeedSnapshot.id).where(FeedSnapshot.feed_id == f.id,
                                                              FeedSnapshot.status.in_(("queued", "running"))))).first()
    if running:
        raise AppError(409, "already_running", "This feed is already being generated")
    snap = await feeds.create_snapshot(db, f, "manual")
    await db.commit()
    feeds.dispatch_snapshot(snap.id)
    return feeds.snapshot_dict(snap)


@router.get("/feeds/{feed_id}/snapshots")
async def snapshots(feed_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    f = await _feed(db, ctx, feed_id)
    rows = (await db.execute(select(FeedSnapshot).where(FeedSnapshot.feed_id == f.id)
                             .order_by(FeedSnapshot.version.desc()).limit(50))).scalars().all()
    return {"items": [feeds.snapshot_dict(s) for s in rows]}


async def _snapshot(db, ctx, snapshot_id) -> FeedSnapshot:
    s = (await db.execute(select(FeedSnapshot).where(FeedSnapshot.id == _uuid(snapshot_id, "Snapshot"),
                                                     FeedSnapshot.business_id == ctx.business_id))).scalar_one_or_none()
    if s is None:
        raise not_found("Snapshot")
    return s


@router.get("/feed-snapshots/{snapshot_id}")
async def snapshot(snapshot_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return feeds.snapshot_dict(await _snapshot(db, ctx, snapshot_id))


@router.get("/feed-snapshots/{snapshot_id}/download")
async def download_snapshot(snapshot_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await _snapshot(db, ctx, snapshot_id)
    if s.status != "completed":
        raise AppError(409, "not_ready", "This snapshot has not completed")
    return Response(content=s.content or "", media_type=s.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{s.filename}"', "ETag": f'"{s.checksum}"'})


@router.post("/feeds/{feed_id}/tokens", status_code=201)
async def create_token(feed_id: str, body: FeedTokenIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    f = await _feed(db, ctx, feed_id)
    if f.archived_at is not None:
        raise AppError(409, "archived", "This feed is archived")
    if body.expires_at is not None and body.expires_at <= now_utc():
        raise AppError(422, "validation_error", "expires_at must be in the future")
    tok, secret = await feeds.issue_token(db, f, body.name, body.expires_at, body.rate_limit_per_minute)
    record(db, ctx.business_id, actor_from_ctx(ctx), "feed_token.created", "feed_token", tok.id,
           f"Feed URL '{tok.name}' created for '{f.name}'", {"prefix": tok.prefix})
    await db.commit()
    return {**feeds.token_dict(tok), "url": feeds.feed_url(secret),
            "note": "Copy this URL now. The token is stored hashed and cannot be shown again."}


async def _token(db, ctx, token_id) -> FeedToken:
    t = (await db.execute(select(FeedToken).where(FeedToken.id == _uuid(token_id, "Feed URL"),
                                                  FeedToken.business_id == ctx.business_id))).scalar_one_or_none()
    if t is None:
        raise not_found("Feed URL")
    return t


@router.post("/feed-tokens/{token_id}/rotate", status_code=201)
async def rotate_token(token_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    old = await _token(db, ctx, token_id)
    if old.revoked_at is not None:
        raise AppError(409, "revoked", "This feed URL is revoked; create a new one")
    f = await db.get(FeedConfiguration, old.feed_id)
    tok, secret = await feeds.issue_token(db, f, old.name, old.expires_at, old.rate_limit_per_minute, rotated_from=old)
    old.revoked_at = now_utc()
    record(db, ctx.business_id, actor_from_ctx(ctx), "feed_token.rotated", "feed_token", tok.id,
           f"Feed URL '{tok.name}' rotated ({old.prefix}… revoked)", {"old_prefix": old.prefix, "prefix": tok.prefix})
    await db.commit()
    return {**feeds.token_dict(tok), "url": feeds.feed_url(secret), "note": "The previous URL stopped working immediately."}


@router.post("/feed-tokens/{token_id}/revoke")
async def revoke_token(token_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    t = await _token(db, ctx, token_id)
    if t.revoked_at is None:
        t.revoked_at = now_utc()
        record(db, ctx.business_id, actor_from_ctx(ctx), "feed_token.revoked", "feed_token", t.id,
               f"Feed URL '{t.name}' ({t.prefix}…) revoked")
        await db.commit()
    return feeds.token_dict(t)


# ================================================================ webhooks
async def _sub(db, ctx, sub_id) -> WebhookSubscription:
    s = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == _uuid(sub_id, "Webhook"),
                                                            WebhookSubscription.business_id == ctx.business_id))).scalar_one_or_none()
    if s is None:
        raise not_found("Webhook")
    return s


@router.get("/webhooks")
async def list_webhooks(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    subs = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.business_id == ctx.business_id)
                             .order_by(WebhookSubscription.created_at.desc()))).scalars().all()
    return {"items": [webhooks.subscription_dict(s) for s in subs], "event_types": list(webhooks.EVENT_TYPES),
            "signing": {"scheme": "Standard Webhooks (HMAC-SHA256)",
                        "headers": ["webhook-id", "webhook-timestamp", "webhook-signature"],
                        "signed_content": "{webhook-id}.{webhook-timestamp}.{raw body}",
                        "tolerance_seconds": webhooks.TOLERANCE_SECONDS},
            "retry_policy": {"max_attempts": webhooks.MAX_ATTEMPTS, "base_delay_seconds": webhooks.BASE_DELAY,
                             "backoff": "exponential (x2) with ±10% jitter, capped at 6 hours"}}


@router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    try:
        url = await webhooks.validate_destination(str(body.url))
        events = webhooks.validate_events(body.events)
    except webhooks.WebhookError as e:
        raise AppError(422, "invalid_webhook", str(e))
    secret = webhooks.new_secret()
    sub = WebhookSubscription(business_id=ctx.business_id, url=url, events=events, description=body.description,
                              secret_encrypted=webhooks.store_secret(secret), secret_prefix=secret[:10], is_active=True)
    db.add(sub)
    await db.flush()
    record(db, ctx.business_id, actor_from_ctx(ctx), "webhook.created", "webhook", sub.id,
           f"Webhook to {url} created ({', '.join(events)})")
    await db.commit()
    return {**webhooks.subscription_dict(sub), "secret": secret,
            "note": "Copy this signing secret now. It cannot be shown again; rotate it if it is lost."}


@router.patch("/webhooks/{sub_id}")
async def update_webhook(sub_id: str, body: WebhookUpdateIn, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    s = await _sub(db, ctx, sub_id)
    d = body.model_dump(exclude_unset=True)
    if d.get("events") is not None:
        try:
            s.events = webhooks.validate_events(d["events"])
        except webhooks.WebhookError as e:
            raise AppError(422, "invalid_webhook", str(e))
    if "description" in d:
        s.description = d["description"]
    if d.get("is_active") is not None:
        s.is_active = d["is_active"]
        if s.is_active:
            s.consecutive_failures = 0
    record(db, ctx.business_id, actor_from_ctx(ctx), "webhook.updated", "webhook", s.id, f"Webhook to {s.url} updated", d)
    await db.commit()
    return webhooks.subscription_dict(s)


@router.delete("/webhooks/{sub_id}")
async def delete_webhook(sub_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await _sub(db, ctx, sub_id)
    s.is_active = False
    record(db, ctx.business_id, actor_from_ctx(ctx), "webhook.disabled", "webhook", s.id, f"Webhook to {s.url} disabled")
    await db.commit()
    return webhooks.subscription_dict(s)


@router.post("/webhooks/{sub_id}/rotate-secret")
async def rotate_secret(sub_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await _sub(db, ctx, sub_id)
    secret = webhooks.new_secret()
    s.previous_secret_encrypted = s.secret_encrypted
    s.previous_secret_expires_at = now_utc() + webhooks.ROTATION_GRACE
    s.secret_encrypted, s.secret_prefix = webhooks.store_secret(secret), secret[:10]
    record(db, ctx.business_id, actor_from_ctx(ctx), "webhook.secret_rotated", "webhook", s.id,
           f"Signing secret of the webhook to {s.url} rotated (old secret also signs for 24 hours)")
    await db.commit()
    return {**webhooks.subscription_dict(s), "secret": secret,
            "note": "Deliveries are signed with both the new and the previous secret for 24 hours."}


@router.post("/webhooks/{sub_id}/test")
async def test_webhook(sub_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await _sub(db, ctx, sub_id)
    prof, scope = await _scope(db, ctx)
    d = await webhooks.test_event(db, s, scope.merchant_ref())
    await db.commit()
    return webhooks.delivery_dict(d)


@router.get("/webhooks/{sub_id}/deliveries")
async def deliveries(sub_id: str, status: str | None = Query(None, pattern="^(pending|delivering|succeeded|failed)$"),
                     page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await _sub(db, ctx, sub_id)
    stmt = select(WebhookDelivery).where(WebhookDelivery.subscription_id == s.id)
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(WebhookDelivery.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [webhooks.delivery_dict(d) for d in rows], "total": total, "page": page, "page_size": page_size}


async def _delivery(db, ctx, delivery_id) -> WebhookDelivery:
    d = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.id == _uuid(delivery_id, "Delivery"),
                                                        WebhookDelivery.business_id == ctx.business_id))).scalar_one_or_none()
    if d is None:
        raise not_found("Delivery")
    return d


@router.get("/webhook-deliveries/{delivery_id}")
async def delivery(delivery_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return webhooks.delivery_dict(await _delivery(db, ctx, delivery_id), include_payload=True)


@router.post("/webhook-deliveries/{delivery_id}/replay", status_code=201)
async def replay(delivery_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    d = await _delivery(db, ctx, delivery_id)
    if d.status in ("pending", "delivering"):
        raise AppError(409, "in_progress", "This delivery is still being attempted")
    if d.event_type == webhooks.TEST_EVENT:
        raise AppError(409, "test_event", "Test events cannot be replayed; send a new test")
    new = await webhooks.replay(db, d)
    record(db, ctx.business_id, actor_from_ctx(ctx), "webhook.replayed", "webhook", d.subscription_id,
           f"Event {d.event_type} ({d.event_id[:8]}) queued for redelivery")
    await db.commit()
    return webhooks.delivery_dict(new)


# ================================================================ AI platforms
@router.get("/platforms")
async def list_platforms(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prof = await get_profile(db, ctx.business)
    out = [await platforms.channel_dict(db, await platforms.get_channel(db, ctx.business_id, k), prof)
           for k in platforms.PROVIDERS]
    await db.commit()
    return {"items": out, "statuses": list(platforms.STATUSES),
            "disclaimer": "Generating a feed or JSON-LD does not by itself make products appear in ChatGPT, Perplexity, "
                          "Google or any other AI platform. Each platform decides inclusion under its own programme."}


def _provider(provider: str):
    if provider not in platforms.PROVIDERS:
        raise not_found("Platform")
    return platforms.PROVIDERS[provider]


@router.put("/platforms/{provider}")
async def update_platform(provider: str, body: PlatformIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prov = _provider(provider)
    ch = await platforms.get_channel(db, ctx.business_id, provider)
    d = body.model_dump(exclude_unset=True)
    cfg = dict(ch.config or {})
    if d.get("enabled") is not None:
        cfg["enabled"] = d["enabled"]
    if "notes" in d:
        cfg["notes"] = d["notes"]
    ch.config = cfg
    if "feed_id" in d:
        if d["feed_id"]:
            f = await _feed(db, ctx, d["feed_id"])
            if prov.feed_profile and f.profile != prov.feed_profile:
                raise AppError(422, "wrong_profile", f"{prov.name} needs a feed with the '{prov.feed_profile}' profile")
            ch.feed_id = f.id
        else:
            ch.feed_id = None
    record(db, ctx.business_id, actor_from_ctx(ctx), "platform.updated", "platform", ch.id, f"{prov.name} channel updated", d)
    prof = await get_profile(db, ctx.business)
    out = await platforms.channel_dict(db, ch, prof)
    await db.commit()
    return out


@router.post("/platforms/{provider}/application")
async def platform_application(provider: str, body: PlatformApplicationIn, ctx: AuthContext = Depends(current_auth),
                               db: AsyncSession = Depends(get_db)):
    """The merchant reports that they applied to the platform's programme (AgentReady cannot verify this)."""
    prov = _provider(provider)
    if provider in ("perplexity", "mcp_clients"):
        raise AppError(409, "not_applicable", f"{prov.name} has no application process to record")
    ch = await platforms.get_channel(db, ctx.business_id, provider)
    ch.application_submitted_at = now_utc() if body.submitted else None
    record(db, ctx.business_id, actor_from_ctx(ctx), "platform.application_reported", "platform", ch.id,
           f"Merchant reported {'submitting' if body.submitted else 'withdrawing'} the {prov.name} application")
    prof = await get_profile(db, ctx.business)
    out = await platforms.channel_dict(db, ch, prof)
    await db.commit()
    return out


@router.post("/platforms/{provider}/validate")
async def validate_platform(provider: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Generate the platform-profile feed now and record its validation report (real rows, real errors)."""
    prov = _provider(provider)
    from app.models import ChannelSyncRun

    ch = await platforms.get_channel(db, ctx.business_id, provider)
    if not prov.feed_profile:
        run = ChannelSyncRun(business_id=ctx.business_id, channel_id=ch.id, action="validate", status="blocked",
                             message=prov.capabilities().blocked_reason)
        db.add(run)
        await db.commit()
        raise AppError(409, "not_supported", prov.capabilities().blocked_reason or "No feed format for this platform")
    if ch.feed_id is None:
        f = FeedConfiguration(business_id=ctx.business_id, name=f"{prov.name} feed", profile=prov.feed_profile,
                              format=prov.feed_format, feed_type="full", schedule="daily", max_age_hours=24,
                              next_run_at=feeds.next_run("daily"))
        db.add(f)
        await db.flush()
        ch.feed_id = f.id
    f = await db.get(FeedConfiguration, ch.feed_id)
    snap = await feeds.create_snapshot(db, f, "manual")
    await db.commit()
    snap = await feeds.run_snapshot(db, snap.id)
    ch = await platforms.get_channel(db, ctx.business_id, provider)
    v = snap.validation or {}
    ch.last_validated_at = now_utc()
    ch.last_validation = {"valid": snap.status == "completed" and v.get("valid", False), "snapshot_id": str(snap.id),
                          "records": snap.record_count, "excluded": snap.excluded_count, "errors": (v.get("errors") or [])[:50],
                          "warnings": (v.get("warnings") or [])[:50], "error": snap.error}
    ch.last_error = snap.error
    run = ChannelSyncRun(business_id=ctx.business_id, channel_id=ch.id, action="validate",
                         status="completed" if snap.status == "completed" else "failed", products=snap.total,
                         valid=snap.record_count, invalid=snap.excluded_count,
                         message=snap.error or f"{snap.record_count} valid rows, {snap.excluded_count} excluded")
    db.add(run)
    prof = await get_profile(db, ctx.business)
    out = await platforms.channel_dict(db, ch, prof)
    await db.commit()
    return out


@router.post("/platforms/{provider}/submit")
async def submit_platform(provider: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prov = _provider(provider)
    from app.models import ChannelSyncRun

    ch = await platforms.get_channel(db, ctx.business_id, provider)
    status, message = await prov.submit_catalog(db, ch)
    db.add(ChannelSyncRun(business_id=ctx.business_id, channel_id=ch.id, action="submit", status=status, message=message))
    await db.commit()
    if status == "blocked":
        raise AppError(409, "blocked", message)
    return {"status": status, "message": message}


# ================================================================ analytics, agent API, checkout
@router.get("/analytics")
async def analytics_view(days: int = Query(30, ge=1, le=365), channel: str | None = Query(None, pattern="^(agent_api|mcp|feeds|webhooks|checkout)$"),
                         consumer: str | None = Query(None, max_length=64),
                         ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    prof, scope = await _scope(db, ctx)
    await db.commit()
    data = await analytics.summary(db, ctx.business_id, days, channel, consumer)
    data["derived"] = {"freshness": await analytics.freshness(db, ctx.business_id, eligibility_clause(scope, "agent_api"))}
    return data


@router.get("/agent-api")
async def agent_api_info(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    from app.api.v3.agents import capability_document

    prof = await get_profile(db, ctx.business)
    await db.commit()
    keys = (await db.execute(select(ApiKey).where(ApiKey.business_id == ctx.business_id)
                             .order_by(ApiKey.revoked_at.is_not(None), ApiKey.created_at.desc()))).scalars().all()
    since = analytics.since_days(30)
    usage = dict((await db.execute(select(DistributionEvent.consumer_id, func.count()).where(
        DistributionEvent.business_id == ctx.business_id, DistributionEvent.consumer_type == "api_key",
        DistributionEvent.created_at >= since).group_by(DistributionEvent.consumer_id))).all())
    daily = (await db.execute(select(ApiKeyUsage.api_key_id, func.sum(ApiKeyUsage.requests)).where(
        ApiKeyUsage.business_id == ctx.business_id).group_by(ApiKeyUsage.api_key_id))).all()
    total_by_key = {k: n for k, n in daily}
    base = get_settings().api_base_url.rstrip("/")
    from app.api.v1.commerce import key_dict

    return {"capabilities": capability_document(), "merchant_id": prof.slug,
            "channels": {c: channel_open(prof, c)[0] for c in ("agent_api", "mcp")},
            "keys": [{**key_dict(k), "agent_access": "agent:read" in (k.scopes or []),
                      "agent_requests_30d": usage.get(str(k.id), 0), "total_requests": total_by_key.get(k.id, 0)} for k in keys],
            "examples": {
                "search": f'curl -H "X-Catalog-Key: $AGENTREADY_KEY" "{base}/api/v3/agents/search?q=motor&attr=voltage:230%20V&availability=available"',
                "product": f'curl -H "X-Catalog-Key: $AGENTREADY_KEY" "{base}/api/v3/agents/products/<product_id>"',
                "compare": f'curl -X POST -H "X-Catalog-Key: $AGENTREADY_KEY" -H "Content-Type: application/json" -d \'{{"product_ids":["<id1>","<id2>"]}}\' "{base}/api/v3/agents/compare"',
                "mcp_config": {"mcpServers": {"agentready": {"type": "http", "url": f"{base}/api/v3/mcp",
                                                             "headers": {"Authorization": "Bearer ${AGENTREADY_KEY}"}}}},
            }}


@router.get("/checkout-sessions")
async def checkout_sessions(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CheckoutSession).where(CheckoutSession.business_id == ctx.business_id)
                             .order_by(CheckoutSession.created_at.desc()).limit(50))).scalars().all()
    return {"items": [session_dict(s) for s in rows]}


__all__ = ["router", "aware"]
