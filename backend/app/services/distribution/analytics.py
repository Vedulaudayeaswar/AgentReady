"""Distribution analytics: only events AgentReady actually observed (requests it served, feeds it delivered,
webhooks it sent). Nothing here estimates recommendations, impressions, purchases or conversions — AgentReady
cannot see what an AI platform does with the data after it is served."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import String, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (DistributionEvent, FeedSnapshot, Product, WebhookDelivery)
from app.services.commerce.money import aware, now_utc

REQUEST_KINDS = ("search", "product_detail", "pricing", "availability", "variants", "compare", "recommend",
                 "merchants", "categories", "catalog", "capabilities")


@dataclass
class Consumer:
    type: str  # api_key|feed_token|anonymous|merchant|subscription
    id: str | None = None
    label: str | None = None


def record(db: AsyncSession, business_id: uuid.UUID, kind: str, channel: str, consumer: Consumer, *,
           product_id=None, category: str | None = None, status_code: int | None = 200, latency_ms: int | None = None,
           result_count: int | None = None, meta: dict | None = None) -> None:
    db.add(DistributionEvent(
        business_id=business_id, kind=kind, channel=channel, consumer_type=consumer.type, consumer_id=consumer.id,
        consumer_label=(consumer.label or "")[:200] or None, product_id=product_id,
        category=(category or "")[:300] or None, status_code=status_code, latency_ms=latency_ms,
        result_count=result_count, meta={k: (str(v)[:200] if isinstance(v, str) else v) for k, v in (meta or {}).items()},
    ))


def _rate(ok: int, total: int) -> float | None:
    return round(ok / total, 4) if total else None


async def summary(db: AsyncSession, business_id: uuid.UUID, days: int = 30, channel: str | None = None,
                  consumer: str | None = None) -> dict:
    since = now_utc() - timedelta(days=days)
    base = [DistributionEvent.business_id == business_id, DistributionEvent.created_at >= since]
    if channel:
        base.append(DistributionEvent.channel == channel)
    if consumer:
        base.append(DistributionEvent.consumer_id == consumer if consumer != "anonymous" else DistributionEvent.consumer_type == "anonymous")
    by_kind = dict((await db.execute(select(DistributionEvent.kind, func.count()).where(*base)
                                     .group_by(DistributionEvent.kind))).all())
    day = func.substr(cast(DistributionEvent.created_at, String), 1, 10)
    daily_rows = (await db.execute(select(day, DistributionEvent.channel, func.count()).where(*base)
                                   .group_by(day, DistributionEvent.channel).order_by(day))).all()
    daily: dict[str, dict] = {}
    for d, ch, n in daily_rows:
        daily.setdefault(d, {"day": d})[ch] = n
    top_products = (await db.execute(
        select(DistributionEvent.product_id, Product.name, func.count()).join(Product, Product.id == DistributionEvent.product_id)
        .where(*base, DistributionEvent.product_id.is_not(None),
               DistributionEvent.kind.in_(("product_detail", "pricing", "availability", "variants", "compare", "mcp_call")))
        .group_by(DistributionEvent.product_id, Product.name).order_by(func.count().desc()).limit(10))).all()
    top_categories = (await db.execute(
        select(DistributionEvent.category, func.count()).where(*base, DistributionEvent.category.is_not(None))
        .group_by(DistributionEvent.category).order_by(func.count().desc()).limit(10))).all()
    consumers = (await db.execute(
        select(DistributionEvent.consumer_type, DistributionEvent.consumer_id, func.max(DistributionEvent.consumer_label),
               func.count(), func.max(DistributionEvent.created_at))
        .where(*base).group_by(DistributionEvent.consumer_type, DistributionEvent.consumer_id)
        .order_by(func.count().desc()).limit(20))).all()
    errors = (await db.execute(
        select(DistributionEvent).where(*base, DistributionEvent.status_code >= 400)
        .order_by(DistributionEvent.created_at.desc()).limit(20))).scalars().all()
    error_count = (await db.execute(select(func.count()).select_from(DistributionEvent)
                                    .where(*base, DistributionEvent.status_code >= 400))).scalar_one()
    requests = sum(n for k, n in by_kind.items() if k in REQUEST_KINDS or k == "mcp_call")
    lat = (await db.execute(select(func.avg(DistributionEvent.latency_ms)).where(
        *base, DistributionEvent.latency_ms.is_not(None)))).scalar_one()

    fs = (await db.execute(select(func.count(), func.sum(case((FeedSnapshot.status == "completed", 1), else_=0)),
                                  func.sum(case((FeedSnapshot.status == "failed", 1), else_=0)))
                           .where(FeedSnapshot.business_id == business_id, FeedSnapshot.created_at >= since))).one()
    wh = (await db.execute(select(func.count(), func.sum(case((WebhookDelivery.status == "succeeded", 1), else_=0)),
                                  func.sum(case((WebhookDelivery.status == "failed", 1), else_=0)))
                           .where(WebhookDelivery.business_id == business_id, WebhookDelivery.created_at >= since))).one()
    finished_feeds = (fs[1] or 0) + (fs[2] or 0)
    finished_hooks = (wh[1] or 0) + (wh[2] or 0)
    return {
        "period_days": days, "since": since.isoformat(),
        "observed": {
            "agent_requests": requests,
            "by_kind": by_kind,
            "search_requests": by_kind.get("search", 0),
            "product_detail_requests": by_kind.get("product_detail", 0),
            "mcp_tool_calls": by_kind.get("mcp_call", 0),
            "feed_downloads": by_kind.get("feed_download", 0),
            "checkout_sessions": by_kind.get("checkout_session", 0),
            "errors": error_count,
            "avg_latency_ms": round(float(lat), 1) if lat is not None else None,
            "daily": list(daily.values()),
            "top_products": [{"product_id": str(pid), "name": name, "requests": n} for pid, name, n in top_products],
            "top_categories": [{"category": c, "requests": n} for c, n in top_categories],
            "consumers": [{"type": t, "id": cid, "label": label or ("Anonymous public agents" if t == "anonymous" else t),
                           "requests": n, "last_seen": aware(last).isoformat() if last else None}
                          for t, cid, label, n, last in consumers],
            "recent_errors": [{"at": aware(e.created_at).isoformat(), "kind": e.kind, "channel": e.channel,
                               "status": e.status_code, "message": (e.meta or {}).get("error")} for e in errors],
            "feeds": {"generated": fs[1] or 0, "failed": fs[2] or 0, "total": fs[0] or 0,
                      "success_rate": _rate(fs[1] or 0, finished_feeds)},
            "webhooks": {"succeeded": wh[1] or 0, "failed": wh[2] or 0, "total": wh[0] or 0,
                         "success_rate": _rate(wh[1] or 0, finished_hooks)},
        },
        "not_measured": ["AI recommendations", "impressions inside AI platforms", "purchases", "conversions"],
        "note": "Counts are events AgentReady served or sent. What external platforms do with the data afterwards is not "
                "visible to AgentReady and is not estimated.",
    }


async def freshness(db: AsyncSession, business_id: uuid.UUID, product_ids_sql) -> dict:
    """Age of the distributed catalog (derived from real timestamps)."""
    row = (await db.execute(select(func.count(), func.min(Product.updated_at), func.max(Product.updated_at))
                            .where(product_ids_sql))).one()
    now = now_utc()
    oldest, newest = aware(row[1]), aware(row[2])
    return {"distributed_products": row[0],
            "oldest_update": oldest.isoformat() if oldest else None,
            "newest_update": newest.isoformat() if newest else None,
            "oldest_age_hours": round((now - oldest).total_seconds() / 3600, 1) if oldest else None}


def since_days(days: int) -> datetime:
    return now_utc() - timedelta(days=days)
