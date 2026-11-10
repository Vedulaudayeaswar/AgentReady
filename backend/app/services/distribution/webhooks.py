"""Outbound distribution webhooks.

Signing follows the Standard Webhooks convention (https://www.standardwebhooks.com):

    webhook-id:        stable event id (the idempotency key; identical on retries and replays)
    webhook-timestamp: unix seconds of this attempt
    webhook-signature: "v1,<base64 HMAC-SHA256(secret, f'{id}.{timestamp}.{body}')>"  (space-separated list
                       during secret rotation: the new and the previous secret both sign)

Secrets look like ``whsec_<base64>`` and the HMAC key is the base64-decoded part. Consumers should reject
timestamps older than 5 minutes (see ``verify``).

Delivery: events from the transactional outbox are fanned out to matching subscriptions; each delivery is claimed
with a conditional UPDATE before sending (no double sends from concurrent ticks), sent through the SSRF-safe client
(public addresses only, pinned IP, HTTPS, no redirects), and retried with exponential backoff and jitter. Payloads
contain only the public product representation, or a tombstone for products that left distribution.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import secrets
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Business, DistributionProfile, OutboxEvent, Product, WebhookDelivery, WebhookSubscription
from app.services.commerce.money import aware, now_utc
from app.services.distribution import analytics
from app.services.distribution.events import EVENT_TYPES
from app.services.distribution.profile import channel_open, merchant_scope
from app.services.distribution.public import SCHEMA, is_distributable, public_product, removal_reason
from app.services.integrations.crypto import decrypt_json, encrypt_json

log = logging.getLogger("agentready.webhooks")
MAX_ATTEMPTS = 8
BASE_DELAY = 30  # seconds; doubles each attempt (30s, 1m, 2m, 4m, ... capped at 6h)
MAX_DELAY = 6 * 3600
TOLERANCE_SECONDS = 300
ROTATION_GRACE = timedelta(hours=24)
REMOVAL_EVENTS = ("product.unpublished", "product.archived")
TEST_EVENT = "webhook.test"


class WebhookError(ValueError):
    pass


# ------------------------------------------------------------------ secrets & signing
def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def _key(secret: str) -> bytes:
    return base64.b64decode(secret.split("_", 1)[1])


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(_key(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret: str, headers: dict, body: bytes, now: int | None = None) -> bool:
    """Reference verification for consumers (also used by the tests and the walkthrough receiver)."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature", "")
    if not msg_id or not ts or not ts.isdigit():
        return False
    if abs((now or int(time.time())) - int(ts)) > TOLERANCE_SECONDS:
        return False
    expected = sign(secret, msg_id, int(ts), body).split(",", 1)[1]
    return any(hmac.compare_digest(s.split(",", 1)[1], expected) for s in sigs.split() if s.startswith("v1,"))


def _secrets_for(sub: WebhookSubscription) -> list[str]:
    out = [decrypt_json(sub.secret_encrypted)["secret"]]
    if sub.previous_secret_encrypted and sub.previous_secret_expires_at and aware(sub.previous_secret_expires_at) > now_utc():
        out.append(decrypt_json(sub.previous_secret_encrypted)["secret"])
    return out


def store_secret(secret: str) -> str:
    return encrypt_json({"secret": secret})


# ------------------------------------------------------------------ subscriptions
async def validate_destination(url: str) -> str:
    """Static checks at creation time; the address is re-validated (and pinned) on every delivery."""
    s = get_settings()
    parts = urlsplit(url.strip())
    if parts.scheme != "https" and not (s.allow_private_networks and parts.scheme == "http"):
        raise WebhookError("Webhook URLs must use https://")
    if parts.username or parts.password:
        raise WebhookError("Webhook URLs must not contain credentials")
    if parts.fragment:
        raise WebhookError("Webhook URLs must not contain a fragment")
    from app.services.crawler import fetcher as fetcher_mod
    from app.services.crawler.fetcher import BlockedURL, SafeFetcher

    guard = SafeFetcher(s, resolver=fetcher_mod.RESOLVER_OVERRIDE)
    try:
        await guard.check_url(url.strip())
    except BlockedURL as e:
        raise WebhookError(f"Webhook URL rejected: {e}")
    finally:
        await guard.aclose()
    return url.strip()


def validate_events(events: list[str]) -> list[str]:
    ev = list(dict.fromkeys(events or []))
    bad = [e for e in ev if e != "*" and e not in EVENT_TYPES]
    if bad or not ev:
        raise WebhookError(f"Unknown event type(s): {', '.join(bad) or 'none selected'}. Use '*' or: {', '.join(EVENT_TYPES)}")
    return ["*"] if "*" in ev else ev


def subscription_dict(s: WebhookSubscription) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(s.id), "url": s.url, "description": s.description, "events": s.events, "is_active": s.is_active,
            "secret_hint": f"{s.secret_prefix}…", "rotation_grace_until": iso(s.previous_secret_expires_at)
            if s.previous_secret_encrypted else None,
            "consecutive_failures": s.consecutive_failures, "last_delivery_at": iso(s.last_delivery_at),
            "last_success_at": iso(s.last_success_at), "created_at": iso(s.created_at)}


def delivery_dict(d: WebhookDelivery, include_payload: bool = False) -> dict:
    iso = lambda x: aware(x).isoformat() if x else None  # noqa: E731
    out = {"id": str(d.id), "subscription_id": str(d.subscription_id), "event_id": d.event_id, "event_type": d.event_type,
           "product_id": str(d.product_id) if d.product_id else None, "status": d.status, "attempts": d.attempts,
           "max_attempts": d.max_attempts, "next_attempt_at": iso(d.next_attempt_at), "last_status_code": d.last_status_code,
           "last_error": d.last_error, "last_response_ms": d.last_response_ms, "delivered_at": iso(d.delivered_at),
           "replay_of_id": str(d.replay_of_id) if d.replay_of_id else None, "replay_number": d.replay_number,
           "attempt_log": d.attempt_log or [], "created_at": iso(d.created_at)}
    if include_payload:
        out["payload"] = d.payload
    return out


# ------------------------------------------------------------------ fan-out
def _payload(event_id: str, event_type: str, at: datetime, merchant: dict, data: dict) -> dict:
    return {"id": event_id, "type": event_type, "created_at": at.isoformat(), "schema": SCHEMA, "merchant": merchant,
            "data": data}


async def fan_out(db: AsyncSession, limit: int = 500) -> dict:
    """Turn undispatched outbox events into deliveries for matching active subscriptions."""
    events = (await db.execute(select(OutboxEvent).where(OutboxEvent.dispatched_at.is_(None))
                               .order_by(OutboxEvent.created_at).limit(limit))).scalars().all()
    if not events:
        return {"events": 0, "deliveries": 0}
    created = 0
    contexts: dict = {}
    for ev in events:
        ctx = contexts.get(ev.business_id)
        if ctx is None:
            subs = (await db.execute(select(WebhookSubscription).where(
                WebhookSubscription.business_id == ev.business_id, WebhookSubscription.is_active.is_(True)))).scalars().all()
            prof = (await db.execute(select(DistributionProfile).where(
                DistributionProfile.business_id == ev.business_id))).scalar_one_or_none()
            scope = None
            if subs and channel_open(prof, "webhooks")[0]:
                scope = await merchant_scope(db, await db.get(Business, ev.business_id), prof)
            ctx = contexts[ev.business_id] = (subs, scope)
        subs, scope = ctx
        ev.dispatched_at = now_utc()
        if not subs or scope is None:
            continue
        targets = [s for s in subs if "*" in (s.events or []) or ev.event_type in (s.events or [])]
        if not targets:
            continue
        p = await db.get(Product, ev.product_id) if ev.product_id else None
        if p is None:
            continue
        if is_distributable(p, scope, "webhooks"):
            if ev.event_type in REMOVAL_EVENTS:
                continue  # the product is visible again by now; a later 'published' event covers it
            data = {"product": public_product(p, scope)}
        elif ev.event_type in REMOVAL_EVENTS and p.published_at is not None:
            data = {"product": {"id": str(p.id), "removed": True, "reason": removal_reason(p)}}
        else:
            continue  # never tell consumers about products they cannot see
        if ev.variant_id:
            data["variant_id"] = str(ev.variant_id)
        data["changed_fields"] = [f for f in ev.changed_fields or [] if f not in ("cost_price_amount",)]
        payload = _payload(str(ev.id), ev.event_type, aware(ev.created_at), scope.merchant_ref(), data)
        for s in targets:
            try:
                async with db.begin_nested():
                    db.add(WebhookDelivery(business_id=ev.business_id, subscription_id=s.id, event_id=str(ev.id),
                                           event_type=ev.event_type, product_id=ev.product_id, payload=payload,
                                           status="pending", next_attempt_at=now_utc(), max_attempts=MAX_ATTEMPTS))
                created += 1
            except IntegrityError:
                pass  # already fanned out (idempotent)
    await db.commit()
    return {"events": len(events), "deliveries": created}


def backoff(attempt: int) -> float:
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    return delay * random.uniform(0.9, 1.1)


async def send(db: AsyncSession, delivery: WebhookDelivery, sub: WebhookSubscription) -> bool:
    from app.services.integrations.http import IntegrationHTTPError, SafeAPIClient

    body = json.dumps(delivery.payload, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    ts = int(time.time())
    sigs = " ".join(sign(sec, delivery.event_id, ts, body) for sec in _secrets_for(sub))
    headers = {"Content-Type": "application/json", "User-Agent": "AgentReady-Webhooks/1.0", "webhook-id": delivery.event_id,
               "webhook-timestamp": str(ts), "webhook-signature": sigs}
    started = time.monotonic()
    client = SafeAPIClient(timeout=10.0, retries=0)
    client.allow_http = get_settings().allow_private_networks
    status, error = None, None
    try:
        resp = await client.request("POST", sub.url, headers=headers, content=body)
        status = resp.status
        if not 200 <= resp.status < 300:
            error = f"HTTP {resp.status}"
    except IntegrationHTTPError as e:
        status, error = e.status, str(e).replace("Store", "Destination").replace("store", "destination")
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}"
    finally:
        await client.aclose()
    ms = int((time.monotonic() - started) * 1000)
    now = now_utc()
    delivery.attempts += 1
    delivery.last_status_code, delivery.last_error, delivery.last_response_ms = status, error, ms
    delivery.attempt_log = [*(delivery.attempt_log or []), {"at": now.isoformat(), "status": status, "error": error, "ms": ms}][-20:]
    sub.last_delivery_at = now
    ok = error is None
    if ok:
        delivery.status, delivery.delivered_at, delivery.next_attempt_at = "succeeded", now, None
        sub.consecutive_failures, sub.last_success_at = 0, now
    elif delivery.attempts >= delivery.max_attempts:
        delivery.status, delivery.next_attempt_at = "failed", None
        sub.consecutive_failures += 1
    else:
        delivery.status = "pending"
        delivery.next_attempt_at = now + timedelta(seconds=backoff(delivery.attempts))
        sub.consecutive_failures += 1
    if ok or delivery.status == "failed":
        analytics.record(db, delivery.business_id, "webhook_delivered" if ok else "webhook_failed", "webhooks",
                         analytics.Consumer("subscription", str(sub.id), urlsplit(sub.url).hostname),
                         product_id=delivery.product_id, status_code=status or (200 if ok else 599), latency_ms=ms,
                         meta={"event": delivery.event_type, "attempts": delivery.attempts, **({"error": error} if error else {})})
    return ok


async def deliver_due(db: AsyncSession, limit: int = 50, at: datetime | None = None) -> dict:
    at = at or now_utc()
    due = (await db.execute(select(WebhookDelivery.id).where(WebhookDelivery.status == "pending",
                                                             WebhookDelivery.next_attempt_at <= at)
                            .order_by(WebhookDelivery.next_attempt_at).limit(limit))).scalars().all()
    sent = failed = 0
    for did in due:
        claim = await db.execute(update(WebhookDelivery).where(WebhookDelivery.id == did, WebhookDelivery.status == "pending")
                                 .values(status="delivering"))
        await db.commit()
        if claim.rowcount != 1:
            continue
        d = await db.get(WebhookDelivery, did, populate_existing=True)
        sub = await db.get(WebhookSubscription, d.subscription_id)
        if sub is None or not sub.is_active:
            d.status, d.last_error = "failed", "Subscription disabled"
            await db.commit()
            continue
        ok = await send(db, d, sub)
        sent += ok
        failed += not ok
        await db.commit()
    return {"sent": sent, "failed_attempts": failed}


async def recover_stuck(db: AsyncSession, older_than: timedelta = timedelta(minutes=10)) -> int:
    """A worker that died mid-send leaves 'delivering'; put such deliveries back in the queue."""
    res = await db.execute(update(WebhookDelivery).where(WebhookDelivery.status == "delivering",
                                                         WebhookDelivery.created_at < now_utc() - older_than,
                                                         WebhookDelivery.delivered_at.is_(None))
                           .values(status="pending", next_attempt_at=now_utc()))
    await db.commit()
    return res.rowcount or 0


async def tick(db: AsyncSession) -> dict:
    out = await fan_out(db)
    out.update(await deliver_due(db))
    return out


async def replay(db: AsyncSession, d: WebhookDelivery) -> WebhookDelivery:
    """Re-send an event with the SAME webhook-id so consumers can de-duplicate it."""
    n = (await db.execute(select(WebhookDelivery.replay_number).where(
        WebhookDelivery.subscription_id == d.subscription_id, WebhookDelivery.event_id == d.event_id)
        .order_by(WebhookDelivery.replay_number.desc()).limit(1))).scalar_one()
    new = WebhookDelivery(business_id=d.business_id, subscription_id=d.subscription_id, event_id=d.event_id,
                          replay_number=n + 1, event_type=d.event_type, product_id=d.product_id, payload=d.payload,
                          status="pending", next_attempt_at=now_utc(), max_attempts=MAX_ATTEMPTS, replay_of_id=d.id)
    db.add(new)
    await db.flush()
    return new


async def test_event(db: AsyncSession, sub: WebhookSubscription, merchant: dict) -> WebhookDelivery:
    eid = str(uuid.uuid4())
    d = WebhookDelivery(business_id=sub.business_id, subscription_id=sub.id, event_id=eid, event_type=TEST_EVENT,
                        payload=_payload(eid, TEST_EVENT, now_utc(), merchant, {"message": "AgentReady webhook test"}),
                        status="delivering", next_attempt_at=None, max_attempts=1)
    db.add(d)
    await db.flush()
    await send(db, d, sub)
    return d
