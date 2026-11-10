"""Phase 4 commerce-intelligence models. Every row carries ``business_id``; handlers always filter on it.

Analytics are derived from existing records (products, price history, inventory transactions, sync conflicts,
distribution events). These tables only hold computed results, merchant decisions and generated artefacts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, utcnow


class IntelligenceSettings(UUIDPk, Timestamps, Base):
    """Per-merchant configuration: quality weights, category requirements, freshness thresholds, alerts."""

    __tablename__ = "intelligence_settings"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), unique=True)
    quality_weights: Mapped[dict] = mapped_column(JSONType, default=dict)  # dimension -> weight (missing = default)
    category_requirements: Mapped[dict] = mapped_column(JSONType, default=dict)  # category_id -> [attribute keys]
    stale_after_days: Mapped[int] = mapped_column(Integer, default=90)
    stale_price_days: Mapped[int] = mapped_column(Integer, default=180)
    stale_stock_days: Mapped[int] = mapped_column(Integer, default=30)
    price_change_alert_percent: Mapped[float] = mapped_column(Float, default=30.0)
    alerts: Mapped[dict] = mapped_column(JSONType, default=dict)  # alert type -> {"enabled": bool, "threshold": n}
    notification_frequency: Mapped[str] = mapped_column(String(20), default="immediate")  # immediate|daily
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # when alerts were last evaluated (the "daily" frequency is measured from this, not from the 10-minute
    # intelligence pass; it is also the start of the window of changes the next alerts look at)
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_snapshot_day: Mapped[str | None] = mapped_column(String(10))


class ProductQualityResult(UUIDPk, Base):
    __tablename__ = "product_quality_results"
    __table_args__ = (Index("ix_quality_business_score", "business_id", "score"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True)
    score: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))  # excellent|good|needs_improvement|critical
    dimensions: Mapped[dict] = mapped_column(JSONType, default=dict)
    missing_required: Mapped[list] = mapped_column(JSONType, default=list)
    missing_optional: Mapped[list] = mapped_column(JSONType, default=list)
    invalid: Mapped[list] = mapped_column(JSONType, default=list)
    stale: Mapped[list] = mapped_column(JSONType, default=list)
    blockers: Mapped[list] = mapped_column(JSONType, default=list)
    withheld: Mapped[list] = mapped_column(JSONType, default=list)
    suggestions: Mapped[list] = mapped_column(JSONType, default=list)
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    product_version: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CatalogQualitySnapshot(UUIDPk, Base):
    """One per merchant per day: the history behind quality and freshness trends."""

    __tablename__ = "catalog_quality_snapshots"
    __table_args__ = (UniqueConstraint("business_id", "day", name="uq_quality_snapshot_day"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    day: Mapped[str] = mapped_column(String(10))
    products: Mapped[int] = mapped_column(Integer, default=0)
    average_score: Mapped[float | None] = mapped_column(Float)
    levels: Mapped[dict] = mapped_column(JSONType, default=dict)
    dimensions: Mapped[dict] = mapped_column(JSONType, default=dict)  # dimension -> average 0..100
    categories: Mapped[dict] = mapped_column(JSONType, default=dict)  # path -> {"products", "average"}
    distribution_ready: Mapped[int] = mapped_column(Integer, default=0)
    stale_products: Mapped[int] = mapped_column(Integer, default=0)
    attention_products: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class IntelligenceRecommendation(UUIDPk, Timestamps, Base):
    __tablename__ = "intelligence_recommendations"
    __table_args__ = (UniqueConstraint("business_id", "fingerprint", name="uq_recommendations_fingerprint"),
                      Index("ix_recommendations_business_status", "business_id", "status", "priority"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(200))  # stable identity: kind + scope
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    product_ids: Mapped[list] = mapped_column(JSONType, default=list)
    priority: Mapped[str] = mapped_column(String(10))  # critical|high|medium|low
    priority_rule: Mapped[str] = mapped_column(Text)
    expected_benefit: Mapped[str] = mapped_column(Text)
    suggested_action: Mapped[str] = mapped_column(Text)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    action: Mapped[str | None] = mapped_column(String(40))  # automated action available after confirmation
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|dismissed|applied|resolved
    source: Mapped[str] = mapped_column(String(20), default="rules")  # rules|ai
    model_id: Mapped[str | None] = mapped_column(String(200))
    dismissed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by: Mapped[str | None] = mapped_column(String(320))
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ProductAnomaly(UUIDPk, Base):
    __tablename__ = "product_anomalies"
    __table_args__ = (UniqueConstraint("business_id", "fingerprint", name="uq_anomalies_fingerprint"),
                      Index("ix_anomalies_business_status", "business_id", "status", "detected_at"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(200))
    rule: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))  # high|medium|low
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    suggestion: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|resolved|dismissed
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MerchantNotification(UUIDPk, Base):
    __tablename__ = "merchant_notifications"
    __table_args__ = (Index("ix_notifications_business_status", "business_id", "status", "last_occurred_at"),
                      UniqueConstraint("business_id", "group_key", name="uq_notifications_group"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    group_key: Mapped[str] = mapped_column(String(200))  # repeated issues are grouped into one notification
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10), default="info")  # critical|warning|info
    link: Mapped[str | None] = mapped_column(String(500))
    count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="unread")  # unread|read|dismissed
    email_status: Mapped[str] = mapped_column(String(20), default="not_configured")  # not_configured|queued|sent
    data: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class IntelligenceReport(UUIDPk, Base):
    __tablename__ = "intelligence_reports"
    __table_args__ = (Index("ix_reports_business_created", "business_id", "created_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    format: Mapped[str] = mapped_column(String(10))  # pdf|csv|json
    title: Mapped[str] = mapped_column(String(300))
    period_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    period_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="completed")  # completed|failed
    data: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_type: Mapped[str | None] = mapped_column(String(100))
    filename: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    coverage: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AIConversation(UUIDPk, Timestamps, Base):
    __tablename__ = "ai_conversations"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300))


class AIConversationMessage(UUIDPk, Base):
    __tablename__ = "ai_conversation_messages"
    __table_args__ = (Index("ix_ai_messages_conversation", "conversation_id", "created_at"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))  # user|assistant
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(40))
    context: Mapped[dict] = mapped_column(JSONType, default=dict)  # retrieved facts + referenced record ids
    source: Mapped[str | None] = mapped_column(String(20))  # groq|deterministic
    model_id: Mapped[str | None] = mapped_column(String(200))
    grounding: Mapped[dict] = mapped_column(JSONType, default=dict)  # validation of the model output
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SuggestedProductEdit(UUIDPk, Timestamps, Base):
    """A proposed change. Nothing is applied until the merchant approves it."""

    __tablename__ = "suggested_product_edits"
    __table_args__ = (Index("ix_suggested_edits_business_status", "business_id", "status"),)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # description|attributes
    original: Mapped[dict] = mapped_column(JSONType, default=dict)
    proposed: Mapped[dict] = mapped_column(JSONType, default=dict)
    rationale: Mapped[str | None] = mapped_column(Text)
    missing_information: Mapped[list] = mapped_column(JSONType, default=list)
    source: Mapped[str] = mapped_column(String(20))  # groq|deterministic
    model_id: Mapped[str | None] = mapped_column(String(200))
    grounding: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_records: Mapped[dict] = mapped_column(JSONType, default=dict)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_conversation_messages.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|superseded
    product_version: Mapped[int] = mapped_column(Integer, default=0)
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
