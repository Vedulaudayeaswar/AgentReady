"""Phase 2 commerce-management models.

Every table holding merchant data carries ``business_id``; API handlers always filter on it.
History tables (price history, inventory transactions, audit logs) are append-only: the ORM
refuses updates and deletes of existing rows (see ``app.models.__init__``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, utcnow

MONEY = Numeric(14, 2)


# ---------------------------------------------------------------- taxonomy
class Category(UUIDPk, Timestamps, Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("business_id", "path_key", name="uq_categories_business_path"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(1000))  # "Electrical / Motors"
    path_key: Mapped[str] = mapped_column(String(1000))  # lower-cased path, unique per merchant
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AttributeDefinition(UUIDPk, Timestamps, Base):
    __tablename__ = "attribute_definitions"
    __table_args__ = (UniqueConstraint("business_id", "key", name="uq_attribute_definitions_business_key"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    data_type: Mapped[str] = mapped_column(String(20), default="string")  # string|number|boolean|date|enum
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(40))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_values: Mapped[list] = mapped_column(JSONType, default=list)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)  # other keys that mean the same thing
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------- pricing
class PriceHistory(UUIDPk, Base):
    """Immutable record of every price modification."""

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_business_product", "business_id", "product_id", "created_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_variants.id", ondelete="SET NULL"))
    price_type: Mapped[str] = mapped_column(String(20))  # regular|sale|cost
    old_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    new_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))
    # manual | bulk | scheduled | website_sync | conflict_resolution | integration_import | product_created
    change_type: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(20), default="user")  # user|system|integration|api_key
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_label: Mapped[str | None] = mapped_column(String(320))
    bulk_operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bulk_operations.id", ondelete="SET NULL"))
    scheduled_change_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scheduled_price_changes.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ScheduledPriceChange(UUIDPk, Timestamps, Base):
    __tablename__ = "scheduled_price_changes"
    __table_args__ = (Index("ix_scheduled_price_due", "status", "effective_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_variants.id", ondelete="SET NULL"))
    price_type: Mapped[str] = mapped_column(String(20), default="regular")  # regular|sale
    new_amount: Mapped[Decimal | None] = mapped_column(MONEY)  # None with price_type=sale => end the sale
    currency: Mapped[str | None] = mapped_column(String(3))
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled|applying|applied|cancelled|failed
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    bulk_operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bulk_operations.id", ondelete="SET NULL"))
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- inventory
class InventoryTransaction(UUIDPk, Base):
    """Immutable record of every stock change."""

    __tablename__ = "inventory_transactions"
    __table_args__ = (Index("ix_inventory_tx_business_created", "business_id", "created_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_variants.id", ondelete="SET NULL"))
    # set | increase | decrease | mark_out_of_stock | reserve | release | integration_import | initial
    transaction_type: Mapped[str] = mapped_column(String(40))
    quantity_field: Mapped[str] = mapped_column(String(20), default="on_hand")  # on_hand|reserved
    previous_quantity: Mapped[int] = mapped_column(Integer)
    adjustment: Mapped[int] = mapped_column(Integer)
    new_quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(20), default="user")
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_label: Mapped[str | None] = mapped_column(String(320))
    bulk_operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bulk_operations.id", ondelete="SET NULL"))
    related_operation: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- bulk operations
class BulkOperation(UUIDPk, Timestamps, Base):
    __tablename__ = "bulk_operations"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    # queued | running | completed | completed_with_errors | failed
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(64))
    actor_label: Mapped[str | None] = mapped_column(String(320))
    run_mode: Mapped[str] = mapped_column(String(20), default="inline")  # inline|background
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class BulkOperationItem(UUIDPk, Base):
    __tablename__ = "bulk_operation_items"
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bulk_operations.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    product_name: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|succeeded|failed|skipped
    before: Mapped[dict | None] = mapped_column(JSONType)
    after: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------- synchronization
class SyncSchedule(UUIDPk, Timestamps, Base):
    """Scan schedule and synchronization settings for one connected website."""

    __tablename__ = "sync_schedules"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id", ondelete="CASCADE"), unique=True)
    frequency: Mapped[str] = mapped_column(String(20), default="manual")  # manual|daily|weekly|monthly
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    hour: Mapped[int] = mapped_column(Integer, default=3)  # local hour of day
    minute: Mapped[int] = mapped_column(Integer, default=0)
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0 = Monday (weekly)
    day_of_month: Mapped[int] = mapped_column(Integer, default=1)  # 1-28 (monthly)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"))
    last_error: Mapped[str | None] = mapped_column(Text)
    max_pages: Mapped[int | None] = mapped_column(Integer)
    max_products: Mapped[int | None] = mapped_column(Integer)
    extraction_methods: Mapped[list] = mapped_column(JSONType, default=list)  # subset of EXTRACTION_METHODS
    detect: Mapped[dict] = mapped_column(JSONType, default=dict)  # change kind -> bool
    conflict_policy: Mapped[str] = mapped_column(String(30), default="review")  # review|keep_agentready|accept_website
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=False)  # apply non-conflicting changes automatically


class SyncJob(UUIDPk, Timestamps, Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (Index("ix_sync_jobs_business_created", "business_id", "created_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(20))  # website|woocommerce|shopify
    website_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("websites.id", ondelete="SET NULL"))
    integration_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commerce_integrations.id", ondelete="SET NULL"))
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"), index=True)
    source_label: Mapped[str | None] = mapped_column(String(2048))
    direction: Mapped[str] = mapped_column(String(20), default="import")  # import|export
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual|scheduled|webhook
    # queued|running|awaiting_review|completed|completed_with_errors|failed|cancelled
    status: Mapped[str] = mapped_column(String(30), default="queued")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    products_discovered: Mapped[int] = mapped_column(Integer, default=0)
    products_created: Mapped[int] = mapped_column(Integer, default=0)
    products_changed: Mapped[int] = mapped_column(Integer, default=0)
    products_ignored: Mapped[int] = mapped_column(Integer, default=0)
    conflicts_detected: Mapped[int] = mapped_column(Integer, default=0)
    failed_operations: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSONType, default=list)
    summary: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64))


class SyncConflict(UUIDPk, Base):
    __tablename__ = "sync_conflicts"
    __table_args__ = (
        Index("ix_sync_conflicts_business_status", "business_id", "status"),
        # one open conflict per product field; a later scan updates it instead of duplicating it
        Index("uq_sync_conflicts_open_field", "product_id", "field", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    sync_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_jobs.id", ondelete="SET NULL"), index=True)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"))
    source_type: Mapped[str] = mapped_column(String(20), default="website")
    field: Mapped[str] = mapped_column(String(120))
    website_value: Mapped[dict | list | str | int | float | None] = mapped_column(JSONType)
    agentready_value: Mapped[dict | list | str | int | float | None] = mapped_column(JSONType)
    original_value: Mapped[dict | list | str | int | float | None] = mapped_column(JSONType)
    evidence: Mapped[list] = mapped_column(JSONType, default=list)
    suggested_action: Mapped[str] = mapped_column(String(30))
    suggestion_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|resolved
    resolution: Mapped[str | None] = mapped_column(String(30))  # accept_website|keep_agentready|edited|ignored
    resolved_value: Mapped[dict | list | str | int | float | None] = mapped_column(JSONType)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- integrations
class CommerceIntegration(UUIDPk, Timestamps, Base):
    __tablename__ = "commerce_integrations"
    __table_args__ = (UniqueConstraint("business_id", "provider", "store_url", name="uq_integrations_business_store"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # woocommerce|shopify
    name: Mapped[str | None] = mapped_column(String(200))
    store_url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|connected|error|disconnected
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)  # Fernet token; never returned by the API
    credentials_hint: Mapped[str | None] = mapped_column(String(64))  # e.g. "ck_…a1b2"
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    currency: Mapped[str | None] = mapped_column(String(3))
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class IntegrationLink(UUIDPk, Base):
    """Maps an external product/variant to an AgentReady product/variant."""

    __tablename__ = "integration_links"
    __table_args__ = (UniqueConstraint("integration_id", "external_id", "external_variant_id", name="uq_integration_links_external"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commerce_integrations.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_variants.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(200))
    external_variant_id: Mapped[str] = mapped_column(String(200), default="")
    last_pulled_hash: Mapped[str | None] = mapped_column(String(64))
    last_pushed_hash: Mapped[str | None] = mapped_column(String(64))  # echo suppression for webhooks
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class IntegrationWebhookEvent(UUIDPk, Base):
    __tablename__ = "integration_webhook_events"
    __table_args__ = (UniqueConstraint("integration_id", "delivery_id", name="uq_webhook_events_delivery"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commerce_integrations.id", ondelete="CASCADE"))
    delivery_id: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="received")  # received|processed|ignored|failed
    detail: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OAuthState(UUIDPk, Base):
    __tablename__ = "oauth_states"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    state_hash: Mapped[str] = mapped_column(String(128), unique=True)
    shop: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------- API keys & audit
class ApiKey(UUIDPk, Timestamps, Base):
    __tablename__ = "api_keys"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120)
    created_by: Mapped[str | None] = mapped_column(String(64))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))
    is_legacy_default: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiKeyUsage(UUIDPk, Base):
    __tablename__ = "api_key_usage"
    __table_args__ = (UniqueConstraint("api_key_id", "day", name="uq_api_key_usage_day"),)
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    requests: Mapped[int] = mapped_column(Integer, default=0)


class AuditLog(UUIDPk, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_business_created", "business_id", "created_at"),
        Index("ix_audit_business_entity", "business_id", "entity_type", "entity_id"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    actor_type: Mapped[str] = mapped_column(String(20), default="user")  # user|system|api_key|integration
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_label: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    changes: Mapped[dict] = mapped_column(JSONType, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64))
    operation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
