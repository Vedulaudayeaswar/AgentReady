"""Phase 3 request schemas. extra="forbid" everywhere: clients cannot mass-assign internal fields."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

Channel = Literal["agent_api", "mcp", "feeds", "webhooks"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _aware(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _url(v):
    return str(v) if v is not None else None


class DistributionSettingsIn(Strict):
    enabled: bool | None = None
    agent_api_enabled: bool | None = None
    mcp_enabled: bool | None = None
    feeds_enabled: bool | None = None
    webhooks_enabled: bool | None = None
    public_network: bool | None = None
    product_scope: Literal["all_published", "selected"] | None = None
    excluded_category_ids: list[str] | None = Field(None, max_length=500)
    show_prices: bool | None = None
    show_availability: bool | None = None
    public_attribute_keys: list[str] | None = Field(None, max_length=200)
    reset_public_attribute_keys: bool = False
    hidden_attribute_keys: list[str] | None = Field(None, max_length=200)
    expires_at: datetime | None = None
    clear_expiry: bool = False
    slug: str | None = Field(None, min_length=3, max_length=60, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    public_name: str | None = Field(None, min_length=1, max_length=200)
    public_url: HttpUrl | None = None
    country: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    support_email: str | None = Field(None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    return_policy_url: HttpUrl | None = None
    privacy_policy_url: HttpUrl | None = None
    terms_url: HttpUrl | None = None
    checkout_enabled: bool | None = None

    _a = field_validator("expires_at")(lambda cls, v: _aware(v))


class ProductDistributionIn(Strict):
    distribution_enabled: bool | None = None
    inherit: bool = False  # true: clear distribution_enabled (follow the merchant profile)
    distribution_channels: list[Channel] | None = None
    all_channels: bool = False
    public_description: str | None = Field(None, max_length=20000)
    clear_public_description: bool = False
    public_images: list[HttpUrl] | None = Field(None, max_length=30)
    clear_public_images: bool = False
    public_attribute_keys: list[str] | None = Field(None, max_length=200)
    clear_public_attribute_keys: bool = False


class BulkDistributionIn(Strict):
    product_ids: list[str] = Field(min_length=1, max_length=1000)
    distribution_enabled: bool | None = None
    inherit: bool = False


class CategoryDistributionIn(Strict):
    distributed: bool


class FeedIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    profile: Literal["agentready", "openai", "google"] = "agentready"
    format: Literal["json", "jsonl", "csv", "jsonld", "xml"] = "json"
    feed_type: Literal["full", "incremental", "availability", "pricing", "category", "variant"] = "full"
    category_id: str | None = None
    schedule: Literal["manual", "hourly", "daily", "weekly"] = "manual"
    max_age_hours: int = Field(24, ge=1, le=24 * 30)


class FeedUpdateIn(Strict):
    name: str | None = Field(None, min_length=1, max_length=200)
    schedule: Literal["manual", "hourly", "daily", "weekly"] | None = None
    max_age_hours: int | None = Field(None, ge=1, le=24 * 30)


class FeedTokenIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None
    rate_limit_per_minute: int = Field(60, ge=1, le=1000)

    _a = field_validator("expires_at")(lambda cls, v: _aware(v))


class WebhookIn(Strict):
    url: HttpUrl
    events: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=20)
    description: str | None = Field(None, max_length=300)


class WebhookUpdateIn(Strict):
    events: list[str] | None = Field(None, min_length=1, max_length=20)
    description: str | None = Field(None, max_length=300)
    is_active: bool | None = None


class PlatformIn(Strict):
    enabled: bool | None = None
    feed_id: str | None = None
    notes: str | None = Field(None, max_length=1000)


class PlatformApplicationIn(Strict):
    submitted: bool


class CompareIn(Strict):
    product_ids: list[str] = Field(min_length=2, max_length=10)


class RecommendIn(Strict):
    request: str = Field(min_length=2, max_length=500)
    interpret: Literal["deterministic", "ai"] = "deterministic"
    max_price: float | None = Field(None, ge=0)
    min_price: float | None = Field(None, ge=0)
    currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    category: str | None = Field(None, max_length=300)
    available_only: bool = True
    merchant: str | None = Field(None, max_length=80)
    limit: int = Field(5, ge=1, le=20)


class CartItemIn(Strict):
    product_id: str = Field(min_length=1, max_length=64)
    variant_id: str | None = Field(None, max_length=64)
    quantity: int = Field(1, ge=1, le=100)
    expected_unit_price: str | None = Field(None, max_length=20, pattern=r"^\d+(\.\d{1,2})?$")


class CartIn(Strict):
    items: list[CartItemIn] = Field(min_length=1, max_length=50)


class CheckoutIn(Strict):
    expected_subtotal: str | None = Field(None, max_length=20, pattern=r"^\d+(\.\d{1,2})?$")
    expected_currency: str | None = Field(None, pattern="^[A-Z]{3}$")


__all__ = [n for n in dir() if n.endswith("In")] + ["_url"]
