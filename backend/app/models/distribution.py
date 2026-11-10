"""Phase 3 distribution models: agent-facing distribution, feeds, webhooks, checkout, platform channels, analytics.

Every table holding merchant data carries ``business_id``. Secrets (feed tokens) are stored as SHA-256 hashes;
webhook signing secrets and platform credentials are Fernet-encrypted (they must be recoverable to sign/send).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, NullableJSON, Timestamps, UTCDateTime, UUIDPk, utcnow

MONEY = Numeric(14, 2)
CHANNELS = ("agent_api", "mcp", "feeds", "webhooks")


class DistributionProfile(UUIDPk, Timestamps, Base):
    """Merchant-level distribution settings (one per business). Everything defaults to *off*."""

    __tablename__ = "distribution_profiles"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # master switch for public distribution
    agent_api_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    feeds_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    webhooks_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # anonymous agents may discover this merchant through the public network (no API key)
    public_network: Mapped[bool] = mapped_column(Boolean, default=False)
    product_scope: Mapped[str] = mapped_column(String(20), default="all_published")  # all_published|selected
    excluded_category_ids: Mapped[list] = mapped_column(JSONType, default=list)
    show_prices: Mapped[bool] = mapped_column(Boolean, default=True)
    show_availability: Mapped[bool] = mapped_column(Boolean, default=True)
    public_attribute_keys: Mapped[list | None] = mapped_column(NullableJSON)  # None = all attributes
    hidden_attribute_keys: Mapped[list] = mapped_column(JSONType, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    slug: Mapped[str] = mapped_column(String(80), unique=True)  # public merchant reference
    public_name: Mapped[str | None] = mapped_column(String(200))
    public_url: Mapped[str | None] = mapped_column(String(2048))
    country: Mapped[str | None] = mapped_column(String(2))
    support_email: Mapped[str | None] = mapped_column(String(320))
    return_policy_url: Mapped[str | None] = mapped_column(String(2048))
    privacy_policy_url: Mapped[str | None] = mapped_column(String(2048))
    terms_url: Mapped[str | None] = mapped_column(String(2048))
    checkout_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------- feeds
class FeedConfiguration(UUIDPk, Timestamps, Base):
    __tablename__ = "feed_configurations"
    __table_args__ = (Index("ix_feed_configs_due", "next_run_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    profile: Mapped[str] = mapped_column(String(20), default="agentready")  # agentready|openai|google
    format: Mapped[str] = mapped_column(String(10), default="json")  # json|jsonl|csv|jsonld|xml
    feed_type: Mapped[str] = mapped_column(String(20), default="full")  # full|incremental|availability|pricing|category|variant
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    schedule: Mapped[str] = mapped_column(String(10), default="manual")  # manual|hourly|daily|weekly
    max_age_hours: Mapped[int] = mapped_column(Integer, default=24)  # a snapshot older than this is expired
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    latest_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # newest completed snapshot (no FK: cycle)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FeedSnapshot(UUIDPk, Base):
    """One generated, immutable version of a feed."""

    __tablename__ = "feed_snapshots"
    __table_args__ = (UniqueConstraint("feed_id", "version", name="uq_feed_snapshots_version"),
                      Index("ix_feed_snapshots_business_created", "business_id", "created_at"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    feed_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("feed_configurations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|completed|failed
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual|scheduled|on_demand
    total: Mapped[int] = mapped_column(Integer, default=0)  # products considered
    processed: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)  # rows written (incl. variants)
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_count: Mapped[int] = mapped_column(Integer, default=0)  # tombstones (incremental)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)  # failed profile validation
    content: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(100))
    filename: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))  # sha256 of content
    manifest: Mapped[dict] = mapped_column(JSONType, default=dict)  # {product_id: content hash} for incrementals
    validation: Mapped[dict] = mapped_column(JSONType, default=dict)  # {"errors": [...], "warnings": [...]}
    since: Mapped[datetime | None] = mapped_column(UTCDateTime())  # incremental base
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FeedToken(UUIDPk, Timestamps, Base):
    """Revocable secret for a public feed URL. Only the SHA-256 hash is stored."""

    __tablename__ = "feed_tokens"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    feed_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("feed_configurations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("feed_tokens.id", ondelete="SET NULL"))


# ---------------------------------------------------------------- events & webhooks
class OutboxEvent(UUIDPk, Base):
    """Product change captured in the same transaction as the change (transactional outbox)."""

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_pending", "dispatched_at", "created_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    # no foreign keys: the row is written in the same flush as a brand-new product/variant (no insert ordering),
    # and the dispatcher tolerates products deleted since the event.
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    changed_fields: Mapped[list] = mapped_column(JSONType, default=list)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WebhookSubscription(UUIDPk, Timestamps, Base):
    __tablename__ = "webhook_subscriptions"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(String(300))
    events: Mapped[list] = mapped_column(JSONType, default=list)  # event types, or ["*"]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    secret_encrypted: Mapped[str] = mapped_column(Text)
    secret_prefix: Mapped[str] = mapped_column(String(16))
    previous_secret_encrypted: Mapped[str | None] = mapped_column(Text)  # still signed with during rotation grace
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_delivery_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WebhookDelivery(UUIDPk, Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "event_id", "replay_number", name="uq_webhook_delivery_event"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_business_created", "business_id", "created_at"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    subscription_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(64))  # stable idempotency key (webhook-id header), kept on replay
    replay_number: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|delivering|succeeded|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_response_ms: Mapped[int | None] = mapped_column(Integer)
    attempt_log: Mapped[list] = mapped_column(JSONType, default=list)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    replay_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("webhook_deliveries.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- analytics
class DistributionEvent(UUIDPk, Base):
    """An observed distribution event (request served, feed downloaded, webhook delivered, ...). Never inferred."""

    __tablename__ = "distribution_events"
    __table_args__ = (Index("ix_dist_events_business_created", "business_id", "created_at"),
                      Index("ix_dist_events_business_kind", "business_id", "kind", "created_at"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    # search|product_detail|pricing|availability|variants|compare|recommend|merchants|categories|catalog|
    # mcp_call|feed_download|feed_generated|feed_failed|webhook_delivered|webhook_failed|checkout_session|cart|error
    kind: Mapped[str] = mapped_column(String(30))
    channel: Mapped[str] = mapped_column(String(20))  # agent_api|mcp|feeds|webhooks|checkout
    consumer_type: Mapped[str] = mapped_column(String(20), default="anonymous")  # api_key|feed_token|anonymous|merchant|subscription
    consumer_id: Mapped[str | None] = mapped_column(String(64))
    consumer_label: Mapped[str | None] = mapped_column(String(200))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    category: Mapped[str | None] = mapped_column(String(300))
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    result_count: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- checkout foundation
class Cart(UUIDPk, Timestamps, Base):
    __tablename__ = "carts"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|checkout_created|expired
    currency: Mapped[str | None] = mapped_column(String(3))
    items: Mapped[list] = mapped_column(JSONType, default=list)
    subtotal: Mapped[Decimal | None] = mapped_column(MONEY)
    issues: Mapped[list] = mapped_column(JSONType, default=list)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class CheckoutSession(UUIDPk, Timestamps, Base):
    """Auditable hand-off to the merchant's own checkout. AgentReady never takes payment."""

    __tablename__ = "checkout_sessions"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    cart_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))
    # ready (a merchant checkout URL was produced) | expired | failed
    status: Mapped[str] = mapped_column(String(20), default="ready")
    method: Mapped[str] = mapped_column(String(30))  # shopify_cart_permalink|woocommerce_add_to_cart|merchant_product_urls
    checkout_url: Mapped[str | None] = mapped_column(String(4096))
    item_urls: Mapped[list] = mapped_column(JSONType, default=list)
    currency: Mapped[str | None] = mapped_column(String(3))
    items: Mapped[list] = mapped_column(JSONType, default=list)  # verified items (price/availability at creation)
    subtotal: Mapped[Decimal | None] = mapped_column(MONEY)
    payment_status: Mapped[str] = mapped_column(String(20), default="not_handled")  # AgentReady never processes payment
    order_status: Mapped[str] = mapped_column(String(30), default="unknown")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------- AI platform channels
class DistributionChannel(UUIDPk, Timestamps, Base):
    __tablename__ = "distribution_channels"
    __table_args__ = (UniqueConstraint("business_id", "provider", name="uq_distribution_channels_provider"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30))  # openai_chatgpt|google_ucp|perplexity|mcp_clients
    # not_configured|setup_required|pending_verification|connected|syncing|active|failed|disconnected
    status: Mapped[str] = mapped_column(String(30), default="not_configured")
    config: Mapped[dict] = mapped_column(JSONType, default=dict)  # non-secret settings
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    feed_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("feed_configurations.id", ondelete="SET NULL"))
    application_submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # merchant-reported
    last_validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_validation: Mapped[dict] = mapped_column(JSONType, default=dict)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class ChannelSyncRun(UUIDPk, Base):
    __tablename__ = "channel_sync_runs"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("distribution_channels.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20))  # validate|submit|sync|status_check
    status: Mapped[str] = mapped_column(String(20))  # completed|failed|blocked
    products: Mapped[int] = mapped_column(Integer, default=0)
    valid: Mapped[int] = mapped_column(Integer, default=0)
    invalid: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
