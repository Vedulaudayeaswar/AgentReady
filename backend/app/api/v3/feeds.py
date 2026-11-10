"""Public, token-scoped feed URLs: ``GET /api/v3/feeds/{token}``.

The token is the only credential (it is stored hashed; the request log filter redacts it). Each token is bound to
one feed, can expire, has its own rate limit, and can be rotated or revoked by the merchant. An expired snapshot is
regenerated on request for small catalogs; for large catalogs a regeneration is queued and 503 + Retry-After is
returned rather than serving data older than the merchant's refresh policy.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.ratelimit import hit
from app.core.security import hash_opaque_token
from app.db.session import get_db
from app.models import DistributionProfile, FeedConfiguration, FeedSnapshot, FeedToken
from app.services.commerce.money import aware, now_utc
from app.services.distribution import analytics, feeds
from app.services.distribution.profile import channel_open

router = APIRouter(prefix="/feeds", tags=["public feeds"])


@router.get("/{token}")
async def download(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    if not token.startswith("arf_") or len(token) > 100:
        raise AppError(404, "not_found", "Feed not found")
    tok = (await db.execute(select(FeedToken).where(FeedToken.token_hash == hash_opaque_token(token)))).scalar_one_or_none()
    if tok is None or not feeds.token_active(tok):
        raise AppError(404 if tok is None else 410, "not_found" if tok is None else "token_inactive",
                       "Feed not found" if tok is None else "This feed URL was revoked or has expired")
    if not await hit(f"feedtoken:{tok.id}", tok.rate_limit_per_minute or 60):
        raise AppError(429, "rate_limited", "Feed URL rate limit exceeded")
    feed = await db.get(FeedConfiguration, tok.feed_id)
    prof = (await db.execute(select(DistributionProfile).where(DistributionProfile.business_id == tok.business_id))).scalar_one_or_none()
    ok, reason = channel_open(prof, "feeds")
    if feed is None or feed.archived_at is not None or not ok:
        raise AppError(404 if feed is None or feed.archived_at else 403, "feed_unavailable",
                       "Feed not found" if feed is None or feed.archived_at else reason)
    snap = await db.get(FeedSnapshot, feed.latest_snapshot_id) if feed.latest_snapshot_id else None
    expired = snap is None or (snap.expires_at is not None and aware(snap.expires_at) <= now_utc())
    if expired:
        pending = (await db.execute(select(func.count()).select_from(FeedSnapshot).where(
            FeedSnapshot.feed_id == feed.id, FeedSnapshot.status.in_(("queued", "running"))))).scalar_one()
        last_total = snap.total if snap else 0
        if not pending and last_total <= feeds.INLINE_REGENERATE_LIMIT:
            new = await feeds.create_snapshot(db, feed, "on_demand")
            await db.commit()
            snap = await feeds.run_snapshot(db, new.id)
            if snap.status != "completed":
                raise AppError(503, "feed_generation_failed", snap.error or "Feed generation failed")
        else:
            if not pending:
                new = await feeds.create_snapshot(db, feed, "on_demand")
                await db.commit()
                feeds.dispatch_snapshot(new.id)
            raise AppError(503, "feed_refreshing", "The feed is being regenerated; retry shortly",
                           {"retry_after_seconds": 30})
    etag = f'"{snap.checksum}"'
    await db.execute(update(FeedToken).where(FeedToken.id == tok.id).values(
        last_used_at=now_utc(), usage_count=FeedToken.usage_count + 1).execution_options(synchronize_session=False))
    not_modified = request.headers.get("if-none-match") == etag
    analytics.record(db, tok.business_id, "feed_download", "feeds", analytics.Consumer("feed_token", str(tok.id), tok.name),
                     status_code=304 if not_modified else 200, result_count=snap.record_count,
                     meta={"feed": feed.name, "version": snap.version})
    await db.commit()
    headers = {"ETag": etag, "Last-Modified": aware(snap.finished_at).strftime("%a, %d %b %Y %H:%M:%S GMT"),
               "X-Feed-Version": str(snap.version), "X-Feed-Generated-At": aware(snap.finished_at).isoformat(),
               "X-Feed-Expires-At": aware(snap.expires_at).isoformat() if snap.expires_at else "",
               "Cache-Control": "private, max-age=60", "Content-Disposition": f'inline; filename="{snap.filename}"'}
    if not_modified:
        return Response(status_code=304, headers=headers)
    return Response(content=snap.content or "", media_type=snap.content_type, headers=headers)
