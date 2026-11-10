"""ORM models. Every merchant-owned row carries ``business_id`` for isolation."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, NullableJSON, Timestamps, UTCDateTime, UUIDPk, utcnow


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    business: Mapped["Business"] = relationship(back_populates="owner", uselist=False)


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class PasswordResetToken(UUIDPk, Base):
    __tablename__ = "password_reset_tokens"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Business(UUIDPk, Timestamps, Base):
    __tablename__ = "businesses"
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    default_currency: Mapped[str | None] = mapped_column(String(3))
    country: Mapped[str | None] = mapped_column(String(2))
    catalog_api_key_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    catalog_api_key_prefix: Mapped[str | None] = mapped_column(String(16))
    # catalog rules
    enforce_unique_skus: Mapped[bool] = mapped_column(Boolean, default=True)
    default_low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5)

    owner: Mapped[User] = relationship(back_populates="business")


class Website(UUIDPk, Timestamps, Base):
    __tablename__ = "websites"
    __table_args__ = (UniqueConstraint("business_id", "normalized_url", name="uq_websites_business_url"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    normalized_url: Mapped[str] = mapped_column(String(2048))
    domain: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(200))
    max_pages: Mapped[int] = mapped_column(Integer, default=50)
    max_depth: Mapped[int] = mapped_column(Integer, default=3)
    use_sitemap: Mapped[bool] = mapped_column(Boolean, default=True)
    render_js: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


ACTIVE_SCAN_STATUSES = ("queued", "running")


class ScanJob(UUIDPk, Timestamps, Base):
    __tablename__ = "scan_jobs"
    __table_args__ = (
        # At most one active scan per website, enforced by the database (closes the check-then-insert race).
        Index(
            "uq_scan_jobs_one_active_per_website",
            "website_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id", ondelete="CASCADE"), index=True)
    # queued | running | awaiting_review | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    stage_history: Mapped[list] = mapped_column(JSONType, default=list)
    is_rescan: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    pages_discovered: Mapped[int] = mapped_column(Integer, default=0)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    pages_skipped_robots: Mapped[int] = mapped_column(Integer, default=0)
    product_pages_found: Mapped[int] = mapped_column(Integer, default=0)
    products_extracted: Mapped[int] = mapped_column(Integer, default=0)
    ai_calls: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSONType, default=list)
    change_summary: Mapped[dict] = mapped_column(JSONType, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    website: Mapped[Website] = relationship()


class CrawledPage(UUIDPk, Base):
    __tablename__ = "crawled_pages"
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(500))
    page_type: Mapped[str] = mapped_column(String(20), default="other")  # product|listing|other
    product_score: Mapped[float] = mapped_column(Float, default=0.0)
    signals: Mapped[dict] = mapped_column(JSONType, default=dict)
    render_mode: Mapped[str] = mapped_column(String(20), default="http")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Product(UUIDPk, Timestamps, Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_business_match", "business_id", "match_key"),
        Index("ix_products_business_status", "business_id", "quality_status", "approval_status"),
        Index("ix_products_business_updated", "business_id", "updated_at"),
        Index("ix_products_business_lifecycle", "business_id", "lifecycle_status", "publication_status"),
        Index("ix_products_business_stock", "business_id", "stock_status"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    website_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("websites.id", ondelete="SET NULL"), index=True)
    source_page_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crawled_pages.id", ondelete="SET NULL"))
    last_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"))
    match_key: Mapped[str] = mapped_column(String(512))

    name: Mapped[str | None] = mapped_column(String(500))
    sku: Mapped[str | None] = mapped_column(String(200), index=True)
    brand: Mapped[str | None] = mapped_column(String(200))
    manufacturer: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(300), index=True)
    category_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    availability: Mapped[str | None] = mapped_column(String(40))
    product_url: Mapped[str | None] = mapped_column(String(2048))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    gtin: Mapped[str | None] = mapped_column(String(14), index=True)
    mpn: Mapped[str | None] = mapped_column(String(100))
    material: Mapped[str | None] = mapped_column(String(200))
    dimensions: Mapped[dict | None] = mapped_column(JSONType)
    weight: Mapped[dict | None] = mapped_column(JSONType)
    images: Mapped[list] = mapped_column(JSONType, default=list)
    extra: Mapped[dict] = mapped_column(JSONType, default=dict)

    quality_status: Mapped[str] = mapped_column(String(32), default="needs_review")
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    approval_status: Mapped[str] = mapped_column(String(20), default="needs_review")
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    manual_overrides: Mapped[list] = mapped_column(JSONType, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)  # no longer found on the website
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # ---- Phase 2: commerce management
    origin: Mapped[str] = mapped_column(String(20), default="website")  # website|manual|woocommerce|shopify
    product_type: Mapped[str | None] = mapped_column(String(60))
    lifecycle_status: Mapped[str] = mapped_column(String(20), default="active")  # draft|active|archived|discontinued
    publication_status: Mapped[str] = mapped_column(String(20), default="unpublished")  # unpublished|published
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # last time it became published
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # soft delete
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    # pricing (price_amount is the regular price)
    sale_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    sale_starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sale_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cost_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # private: never in public APIs
    tax_class: Mapped[str | None] = mapped_column(String(60))
    tax_included: Mapped[bool | None] = mapped_column(Boolean)
    shipping: Mapped[dict] = mapped_column(JSONType, default=dict)
    seo_title: Mapped[str | None] = mapped_column(String(200))
    seo_description: Mapped[str | None] = mapped_column(String(500))
    # inventory (available = on_hand - reserved)
    track_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    stock_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    stock_reserved: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer)
    backorder_policy: Mapped[str] = mapped_column(String(20), default="deny")  # deny|allow
    inventory_location: Mapped[str | None] = mapped_column(String(200))
    inventory_version: Mapped[int] = mapped_column(Integer, default=0)
    stock_status: Mapped[str] = mapped_column(String(20), default="untracked")
    # the website's value for each tracked field at the last applied scan (conflict detection)
    source_values: Mapped[dict | None] = mapped_column(JSONType)
    # ---- Phase 3: distribution (public overrides never replace internal values or evidence)
    distribution_enabled: Mapped[bool | None] = mapped_column(Boolean)  # None = follow the merchant profile
    distribution_channels: Mapped[list | None] = mapped_column(NullableJSON)  # None = every enabled channel
    public_description: Mapped[str | None] = mapped_column(Text)
    public_images: Mapped[list | None] = mapped_column(NullableJSON)  # None = product images
    public_attribute_keys: Mapped[list | None] = mapped_column(NullableJSON)  # None = profile rules

    attributes: Mapped[list["ProductAttribute"]] = relationship(
        cascade="all, delete-orphan", order_by="ProductAttribute.position", lazy="selectin"
    )
    variants: Mapped[list["ProductVariant"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ProductVariant(UUIDPk, Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        # no two live variants of a product with the same option combination
        Index("uq_variants_live_combination", "product_id", "combination_key", unique=True,
              postgresql_where=text("archived_at IS NULL AND combination_key IS NOT NULL"),
              sqlite_where=text("archived_at IS NULL AND combination_key IS NOT NULL")),
    )
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(200))
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # None => product price
    sale_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    availability: Mapped[str | None] = mapped_column(String(40))
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)  # option values, e.g. {"Size": "M"}
    combination_key: Mapped[str | None] = mapped_column(String(600))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    origin: Mapped[str] = mapped_column(String(20), default="extracted")  # extracted|merchant|integration
    track_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    stock_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    stock_reserved: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer)
    backorder_policy: Mapped[str] = mapped_column(String(20), default="deny")
    inventory_location: Mapped[str | None] = mapped_column(String(200))
    inventory_version: Mapped[int] = mapped_column(Integer, default=0)
    stock_status: Mapped[str] = mapped_column(String(20), default="untracked")
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ProductAttribute(UUIDPk, Base):
    __tablename__ = "product_attributes"
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(200))
    value: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(40))
    raw_value: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="extracted")  # extracted|merchant
    position: Mapped[int] = mapped_column(Integer, default=0)


class ProductEvidence(UUIDPk, Base):
    __tablename__ = "product_evidence"
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"))
    page_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crawled_pages.id", ondelete="SET NULL"))
    field: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict | list | str | int | float | None] = mapped_column(JSONType)
    source_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_fragment: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    extraction_method: Mapped[str] = mapped_column(String(20))  # deterministic|ai|ai_mock|merchant
    inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    model_id: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    verified_in_source: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ValidationIssue(UUIDPk, Timestamps, Base):
    __tablename__ = "validation_issues"
    __table_args__ = (Index("ix_validation_business_status", "business_id", "status"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"))
    code: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10))  # error|warning|info
    field: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|resolved|dismissed
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ScanProductSnapshot(UUIDPk, Base):
    __tablename__ = "scan_product_snapshots"
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    page_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crawled_pages.id", ondelete="SET NULL"))
    match_key: Mapped[str] = mapped_column(String(512))
    change_type: Mapped[str] = mapped_column(String(20))  # new|changed|unchanged|removed
    data: Mapped[dict | None] = mapped_column(JSONType)
    changes: Mapped[list] = mapped_column(JSONType, default=list)
    decision: Mapped[str] = mapped_column(String(20), default="pending")  # pending|applied|skipped
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ExportJob(UUIDPk, Base):
    __tablename__ = "export_jobs"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    format: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="completed")
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    filters: Mapped[dict] = mapped_column(JSONType, default=dict)
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(100))
    content: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AIRequest(UUIDPk, Base):
    __tablename__ = "ai_requests"
    __table_args__ = (Index("ix_ai_requests_cache", "content_hash", "model", "prompt_version"),)
    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))  # success|error|cached
    response_format: Mapped[str | None] = mapped_column(String(20))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    response_json: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


from app.models.commerce import (  # noqa: E402
    ApiKey,
    ApiKeyUsage,
    AttributeDefinition,
    AuditLog,
    BulkOperation,
    BulkOperationItem,
    Category,
    CommerceIntegration,
    IntegrationLink,
    IntegrationWebhookEvent,
    InventoryTransaction,
    OAuthState,
    PriceHistory,
    ScheduledPriceChange,
    SyncConflict,
    SyncJob,
    SyncSchedule,
)
from app.models.distribution import (  # noqa: E402
    Cart,
    ChannelSyncRun,
    CheckoutSession,
    DistributionChannel,
    DistributionEvent,
    DistributionProfile,
    FeedConfiguration,
    FeedSnapshot,
    FeedToken,
    OutboxEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from app.models.intelligence import (  # noqa: E402
    AIConversation,
    AIConversationMessage,
    CatalogQualitySnapshot,
    IntelligenceRecommendation,
    IntelligenceReport,
    IntelligenceSettings,
    MerchantNotification,
    ProductAnomaly,
    ProductQualityResult,
    SuggestedProductEdit,
)


def live_products(business_id):
    """WHERE clauses for products in the merchant's catalog (not soft-deleted, not removed from the website)."""
    return (Product.business_id == business_id, Product.is_removed.is_(False), Product.deleted_at.is_(None))


# Append-only history: the ORM refuses updates/deletes of existing rows.
from sqlalchemy import event  # noqa: E402


def _immutable(mapper, connection, target):
    raise RuntimeError(f"{type(target).__name__} records are immutable")


for _cls in (PriceHistory, InventoryTransaction, AuditLog):
    event.listen(_cls, "before_update", _immutable)
    event.listen(_cls, "before_delete", _immutable)

# Phase 3: capture every product change into the distribution outbox (webhooks) and invalidate public caches
from app.services.distribution import events as _distribution_events  # noqa: E402,F401

__all__ = [
    "ApiKey", "ApiKeyUsage", "AttributeDefinition", "AuditLog", "BulkOperation", "BulkOperationItem", "Category",
    "CommerceIntegration", "IntegrationLink", "IntegrationWebhookEvent", "InventoryTransaction", "OAuthState",
    "PriceHistory", "ScheduledPriceChange", "SyncConflict", "SyncJob", "SyncSchedule", "live_products",
    "Cart", "ChannelSyncRun", "CheckoutSession", "DistributionChannel", "DistributionEvent", "DistributionProfile",
    "FeedConfiguration", "FeedSnapshot", "FeedToken", "OutboxEvent", "WebhookDelivery", "WebhookSubscription",
    "AIConversation", "AIConversationMessage", "CatalogQualitySnapshot", "IntelligenceRecommendation", "IntelligenceReport",
    "IntelligenceSettings", "MerchantNotification", "ProductAnomaly", "ProductQualityResult", "SuggestedProductEdit",
    "AIRequest",
    "Business",
    "CrawledPage",
    "ExportJob",
    "PasswordResetToken",
    "Product",
    "ProductAttribute",
    "ProductEvidence",
    "ProductVariant",
    "RevokedToken",
    "ScanJob",
    "ScanProductSnapshot",
    "User",
    "ValidationIssue",
    "Website",
]
