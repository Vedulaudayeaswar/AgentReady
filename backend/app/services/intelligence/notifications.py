"""Merchant alerts (in-app notification center; email-ready but no provider is configured) and the intelligence job.

Repeated issues are grouped: each alert has a ``group_key`` (e.g. one "price changes today" notification whose count
grows) so merchants are not flooded. Alert types can be disabled and have thresholds; ``daily`` frequency evaluates
alerts at most once a day.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings as app_settings
from app.models import (Business, DistributionEvent, FeedSnapshot, MerchantNotification, PriceHistory, Product, SyncConflict,
                        SyncJob, WebhookDelivery, live_products)
from app.services.commerce.money import aware, now_utc
from app.services.intelligence import anomalies, engine, recommendations

log = logging.getLogger("agentready.intelligence")
ALERTS = {
    "quality_drop": {"label": "Catalog quality drops", "threshold": 5, "unit": "points"},
    "stale_info": {"label": "Product information becomes stale", "threshold": 1, "unit": "products"},
    "out_of_stock": {"label": "Products go out of stock", "threshold": 1, "unit": "products"},
    "price_change": {"label": "Prices change", "threshold": 1, "unit": "changes"},
    "sync_failed": {"label": "Synchronization fails", "threshold": 1, "unit": "failures"},
    "new_conflicts": {"label": "New website conflicts", "threshold": 1, "unit": "conflicts"},
    "feed_failed": {"label": "Feed generation fails", "threshold": 1, "unit": "failures"},
    "api_errors": {"label": "Distribution API errors increase", "threshold": 10, "unit": "errors per hour"},
    "webhook_failures": {"label": "Webhook deliveries fail", "threshold": 1, "unit": "failed deliveries"},
    "quality_blockers": {"label": "Critical quality blockers", "threshold": 1, "unit": "critical products"},
}
EVALUATE_EVERY = timedelta(minutes=10)


def alert_config(settings) -> dict:
    cfg = settings.alerts or {}
    return {k: {"enabled": cfg.get(k, {}).get("enabled", True), "threshold": cfg.get(k, {}).get("threshold", v["threshold"]),
                "label": v["label"], "unit": v["unit"]} for k, v in ALERTS.items()}


def email_channel() -> dict:
    return {"configured": False, "status": "not_configured",
            "note": "No email provider is configured; notifications are shown in the app only."}


async def _upsert(db, business_id, kind, group_key, title, body, severity, link, count, data, accumulate: bool):
    row = (await db.execute(select(MerchantNotification).where(MerchantNotification.business_id == business_id,
                                                               MerchantNotification.group_key == group_key))).scalar_one_or_none()
    now = now_utc()
    if row is None:
        db.add(MerchantNotification(business_id=business_id, kind=kind, group_key=group_key, title=title, body=body,
                                    severity=severity, link=link, count=count, data=data, status="unread",
                                    email_status="not_configured", created_at=now, last_occurred_at=now))
        return True
    new_count = row.count + count if accumulate else count
    if new_count > row.count or row.title != title:
        row.title, row.body, row.data, row.last_occurred_at = title, body, data, now
        if row.status != "dismissed" or new_count > row.count:
            row.status, row.read_at = "unread", None
        row.count = new_count
        return True
    return False


async def evaluate(db: AsyncSession, business: Business, since) -> int:
    s = await engine.get_settings(db, business.id)
    cfg = alert_config(s)
    now = now_utc()
    day = now.date().isoformat()
    made = 0
    on = lambda k: cfg[k]["enabled"]  # noqa: E731
    th = lambda k: cfg[k]["threshold"]  # noqa: E731

    hist = await engine.history(db, business.id, 30)
    if on("quality_drop") and len(hist) >= 2 and hist[-1]["average_score"] is not None and hist[-2]["average_score"] is not None:
        drop = hist[-2]["average_score"] - hist[-1]["average_score"]
        if drop >= th("quality_drop"):
            made += await _upsert(db, business.id, "quality_drop", f"quality_drop:{day}", f"Catalog quality dropped {drop:.1f} points",
                                  f"Average score {hist[-2]['average_score']} on {hist[-2]['day']} → {hist[-1]['average_score']} today.",
                                  "warning", "/app/intelligence/quality", 1, {"drop": drop}, False)
    rows = await engine.results(db, business.id)
    stale = [p for r, p in rows if r.stale]
    if on("stale_info") and len(stale) >= th("stale_info"):
        made += await _upsert(db, business.id, "stale_info", f"stale:{now.isocalendar()[0]}-{now.isocalendar()[1]}",
                              f"{len(stale)} product(s) have stale information", ", ".join(p.name or "?" for p in stale[:5]),
                              "info", "/app/intelligence/quality?filter=stale", len(stale), {"products": [str(p.id) for p in stale[:50]]}, False)
    critical = [p for r, p in rows if r.level == "critical"]
    if on("quality_blockers") and len(critical) >= th("quality_blockers"):
        made += await _upsert(db, business.id, "quality_blockers", f"critical:{day}", f"{len(critical)} product(s) have critical quality",
                              ", ".join(p.name or "?" for p in critical[:5]), "warning", "/app/intelligence/quality?level=critical",
                              len(critical), {"products": [str(p.id) for p in critical[:50]]}, False)
    oos = list((await db.execute(select(Product).where(*live_products(business.id), Product.track_inventory.is_(True),
                                                       Product.stock_status == "out_of_stock"))).scalars())
    if on("out_of_stock") and len(oos) >= th("out_of_stock"):
        made += await _upsert(db, business.id, "out_of_stock", f"oos:{day}", f"{len(oos)} product(s) out of stock",
                              ", ".join(p.name or "?" for p in oos[:5]), "warning", "/app/intelligence/inventory", len(oos),
                              {"products": [str(p.id) for p in oos[:50]]}, False)

    async def count(model, *conds):
        return (await db.execute(select(func.count()).select_from(model).where(model.business_id == business.id, *conds))).scalar_one()

    n = await count(PriceHistory, PriceHistory.created_at > since, PriceHistory.change_type != "product_created")
    if on("price_change") and n >= th("price_change"):
        made += await _upsert(db, business.id, "price_change", f"price:{day}", "Prices changed today",
                              "Price history entries were recorded (manual, bulk, scheduled, website or integration).",
                              "info", "/app/intelligence/pricing", n, {}, True)
    n = await count(SyncJob, SyncJob.status == "failed", SyncJob.updated_at > since)
    if on("sync_failed") and n >= th("sync_failed"):
        made += await _upsert(db, business.id, "sync_failed", f"sync_failed:{day}", "Synchronization failed",
                              "One or more sync jobs failed.", "critical", "/app/sync", n, {}, True)
    n = await count(SyncConflict, SyncConflict.status == "open", SyncConflict.detected_at > since)
    if on("new_conflicts") and n >= th("new_conflicts"):
        made += await _upsert(db, business.id, "new_conflicts", f"conflicts:{day}", "New website conflicts",
                              "The website changed values you manage.", "warning", "/app/sync", n, {}, True)
    n = await count(FeedSnapshot, FeedSnapshot.status == "failed", FeedSnapshot.created_at > since)
    if on("feed_failed") and n >= th("feed_failed"):
        made += await _upsert(db, business.id, "feed_failed", f"feed_failed:{day}", "Feed generation failed",
                              "A feed snapshot failed; see Product feeds for the error.", "critical", "/app/distribution/feeds", n, {}, True)
    n = await count(DistributionEvent, DistributionEvent.status_code >= 400, DistributionEvent.created_at > now - timedelta(hours=1))
    if on("api_errors") and n >= th("api_errors"):
        made += await _upsert(db, business.id, "api_errors", f"api_errors:{now.strftime('%Y-%m-%dT%H')}",
                              f"{n} distribution API errors in the last hour", "Agents received error responses.", "warning",
                              "/app/distribution/analytics", n, {}, False)
    n = await count(WebhookDelivery, WebhookDelivery.status == "failed", WebhookDelivery.created_at > since - timedelta(days=1))
    if on("webhook_failures") and n >= th("webhook_failures"):
        made += await _upsert(db, business.id, "webhook_failures", f"webhooks:{day}", "Webhook deliveries failed",
                              "Some webhook events could not be delivered after all retries.", "warning", "/app/distribution/webhooks",
                              n, {}, False)
    await db.commit()
    return made


async def run_for_business(db: AsyncSession, business: Business, force: bool = False) -> dict:
    """Full intelligence pass for one merchant: incremental quality, daily snapshot, anomalies, recommendations, alerts."""
    out = {"quality": await engine.refresh(db, business, force)}
    await engine.snapshot(db, business)
    out["anomalies"] = await anomalies.scan(db, business.id)
    out["recommendations"] = await recommendations.refresh(db, business)
    s = await engine.get_settings(db, business.id)
    last = aware(s.last_notified_at) if s.last_notified_at else None
    # "daily": alerts are evaluated at most once per 24 h; the window then covers everything since the last
    # evaluation, so nothing is skipped. "immediate": evaluated on every pass.
    wait = s.notification_frequency == "daily" and last is not None and now_utc() - last < timedelta(days=1)
    if wait and not force:
        out["notifications"] = 0
    else:
        out["notifications"] = await evaluate(db, business, last or now_utc() - timedelta(days=1))
        s.last_notified_at = now_utc()
    s.last_evaluated_at = now_utc()
    await db.commit()
    return out


async def tick(db: AsyncSession, limit: int = 20) -> dict:
    """Scheduler entry point: merchants whose intelligence is older than EVALUATE_EVERY (and who have products)."""
    from app.models import IntelligenceSettings

    cutoff = now_utc() - EVALUATE_EVERY
    has_products = select(Product.business_id).where(Product.deleted_at.is_(None)).distinct()
    biz = (await db.execute(select(Business).outerjoin(IntelligenceSettings, IntelligenceSettings.business_id == Business.id)
                            .where(Business.id.in_(has_products),
                                   (IntelligenceSettings.id.is_(None)) | (IntelligenceSettings.last_evaluated_at.is_(None))
                                   | (IntelligenceSettings.last_evaluated_at < cutoff)).limit(limit))).scalars().all()
    done = 0
    for b in biz:
        bid = b.id
        try:
            # Claim the merchant first (conditional update): a second scheduler tick running concurrently
            # (a long tick overlapping the next one) skips it instead of evaluating it twice.
            st = await engine.get_settings(db, b.id)
            await db.commit()
            claim = await db.execute(update(IntelligenceSettings).where(
                IntelligenceSettings.id == st.id,
                (IntelligenceSettings.last_evaluated_at.is_(None)) | (IntelligenceSettings.last_evaluated_at < cutoff))
                .values(last_evaluated_at=now_utc()).execution_options(synchronize_session=False))
            await db.commit()
            if claim.rowcount != 1:
                continue
            await run_for_business(db, b)
            done += 1
        except Exception:  # keep other merchants going
            await db.rollback()
            log.exception("intelligence run failed for business %s", bid)
    return {"merchants": done}


def notification_dict(n: MerchantNotification) -> dict:
    return {"id": str(n.id), "kind": n.kind, "label": ALERTS.get(n.kind, {}).get("label", n.kind), "title": n.title, "body": n.body,
            "severity": n.severity, "link": n.link, "count": n.count, "status": n.status, "email_status": n.email_status,
            "data": n.data, "created_at": aware(n.created_at).isoformat(), "last_occurred_at": aware(n.last_occurred_at).isoformat(),
            "read_at": aware(n.read_at).isoformat() if n.read_at else None}


__all__ = ["evaluate", "run_for_business", "tick", "notification_dict", "alert_config", "email_channel", "uuid", "app_settings"]
