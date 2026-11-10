"""Phase 2 request schemas. All use extra="forbid" so clients cannot mass-assign internal fields."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.api import AttributeIn, AvailabilityLiteral, ImageIn

Money = Decimal


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeasureIn(Strict):
    value: str = Field(min_length=1, max_length=40)
    unit: str | None = Field(default=None, max_length=20)


class ShippingIn(Strict):
    requires_shipping: bool | None = None
    shipping_class: str | None = Field(default=None, max_length=60)
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    notes: str | None = Field(default=None, max_length=500)


def _aware(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValueError("Datetime must include a timezone offset (e.g. 2026-12-01T09:00:00+05:30)")
    return v


class ProductCreate(Strict):
    name: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=20000)
    brand: str | None = Field(default=None, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=200)
    sku: str | None = Field(default=None, max_length=200)
    gtin: str | None = Field(default=None, pattern=r"^\d{8,14}$")
    mpn: str | None = Field(default=None, max_length=100)
    category_id: str | None = None
    product_type: str | None = Field(default=None, max_length=60)
    material: str | None = Field(default=None, max_length=200)
    images: list[ImageIn] = Field(default_factory=list, max_length=20)
    attributes: list[AttributeIn] = Field(default_factory=list, max_length=100)
    regular_price: Money | None = Field(default=None, gt=0, le=Decimal("1000000000"))
    sale_price: Money | None = Field(default=None, gt=0, le=Decimal("1000000000"))
    cost_price: Money | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    availability: AvailabilityLiteral | None = None
    track_inventory: bool = False
    stock_quantity: int | None = Field(default=None, ge=0, le=1_000_000_000)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=1_000_000_000)
    backorder_policy: Literal["deny", "allow"] = "deny"
    inventory_location: str | None = Field(default=None, max_length=200)
    shipping: ShippingIn | None = None
    weight: MeasureIn | None = None
    dimensions: MeasureIn | None = None
    product_url: str | None = Field(default=None, max_length=2048)
    tax_class: str | None = Field(default=None, max_length=60)
    tax_included: bool | None = None
    seo_title: str | None = Field(default=None, max_length=200)
    seo_description: str | None = Field(default=None, max_length=500)
    lifecycle_status: Literal["draft", "active"] = "draft"


class ProductUpdate(Strict):
    """PATCH body. Prices go through the pricing rules (and history); stock through inventory endpoints."""
    name: str | None = Field(default=None, max_length=500)
    sku: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=300)
    category_id: str | None = None
    description: str | None = Field(default=None, max_length=20000)
    price_amount: Money | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    availability: AvailabilityLiteral | None = None
    product_url: str | None = Field(default=None, max_length=2048)
    gtin: str | None = Field(default=None, pattern=r"^\d{8,14}$")
    mpn: str | None = Field(default=None, max_length=100)
    material: str | None = Field(default=None, max_length=200)
    images: list[ImageIn] | None = Field(default=None, max_length=20)
    attributes: list[AttributeIn] | None = Field(default=None, max_length=100)
    approval_status: Literal["needs_review", "approved", "rejected"] | None = None
    # Phase 2
    product_type: str | None = Field(default=None, max_length=60)
    lifecycle_status: Literal["draft", "active", "archived", "discontinued"] | None = None
    publication_status: Literal["unpublished", "published"] | None = None
    shipping: ShippingIn | None = None
    weight: MeasureIn | None = None
    dimensions: MeasureIn | None = None
    seo_title: str | None = Field(default=None, max_length=200)
    seo_description: str | None = Field(default=None, max_length=500)


class PricingIn(Strict):
    regular_price: Money | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    sale_price: Money | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    cost_price: Money | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    sale_starts_at: datetime | None = None
    sale_ends_at: datetime | None = None
    tax_class: str | None = Field(default=None, max_length=60)
    tax_included: bool | None = None
    reason: str | None = Field(default=None, max_length=500)
    variant_id: str | None = None

    _a = field_validator("sale_starts_at", "sale_ends_at")(lambda cls, v: _aware(v))


class ScheduleIn(Strict):
    product_id: str
    variant_id: str | None = None
    price_type: Literal["regular", "sale"] = "regular"
    new_amount: Money | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    effective_at: datetime
    reason: str | None = Field(default=None, max_length=500)

    _a = field_validator("effective_at")(lambda cls, v: _aware(v))


class InventorySettingsIn(Strict):
    track_inventory: bool | None = None
    low_stock_threshold: int | None = Field(default=None, ge=0, le=1_000_000_000)
    backorder_policy: Literal["deny", "allow"] | None = None
    inventory_location: str | None = Field(default=None, max_length=200)
    # optional "set to quantity" in the same call (recorded as a transaction)
    stock_quantity: int | None = Field(default=None, ge=0, le=1_000_000_000)
    reason: str | None = Field(default=None, max_length=500)
    variant_id: str | None = None


class InventoryAdjustIn(Strict):
    product_id: str
    variant_id: str | None = None
    operation: Literal["set", "increase", "decrease", "mark_out_of_stock", "reserve", "release"]
    quantity: int = Field(default=0, ge=0, le=1_000_000_000)
    reason: str | None = Field(default=None, max_length=500)


class VariantIn(Strict):
    options: dict[str, str] = Field(default_factory=dict, max_length=10)
    name: str | None = Field(default=None, max_length=300)
    sku: str | None = Field(default=None, max_length=200)
    regular_price: Money | None = Field(default=None, gt=0, le=Decimal("1000000000"))
    sale_price: Money | None = Field(default=None, gt=0, le=Decimal("1000000000"))
    image_url: str | None = Field(default=None, max_length=2048)
    is_enabled: bool = True
    track_inventory: bool = False
    stock_quantity: int | None = Field(default=None, ge=0, le=1_000_000_000)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=1_000_000_000)
    backorder_policy: Literal["deny", "allow"] = "deny"
    position: int | None = Field(default=None, ge=0, le=10000)

    @field_validator("options")
    @classmethod
    def _opts(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if not str(k).strip() or len(str(k)) > 60 or len(str(val)) > 120:
                raise ValueError("Option names must be 1-60 characters and values at most 120")
        return {str(k).strip(): str(val).strip() for k, val in v.items()}


class VariantUpdate(Strict):
    options: dict[str, str] | None = Field(default=None, max_length=10)
    name: str | None = Field(default=None, max_length=300)
    sku: str | None = Field(default=None, max_length=200)
    image_url: str | None = Field(default=None, max_length=2048)
    is_enabled: bool | None = None
    position: int | None = Field(default=None, ge=0, le=10000)
    regular_price: Money | None = Field(default=None, gt=0, le=Decimal("1000000000"))
    sale_price: Money | None = Field(default=None, gt=0, le=Decimal("1000000000"))
    inherit_price: bool | None = None  # true => use the product's price
    archived: bool | None = None


class CategoryIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    parent_id: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    position: int = Field(default=0, ge=0, le=100000)

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        v = v.strip()
        if "/" in v:
            raise ValueError("Category names cannot contain '/' (use a parent category instead)")
        if not v:
            raise ValueError("Name is required")
        return v


class CategoryUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    position: int | None = Field(default=None, ge=0, le=100000)
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        if v is not None and "/" in v:
            raise ValueError("Category names cannot contain '/'")
        return v.strip() if v else v


class AttributeDefinitionIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    key: str | None = Field(default=None, max_length=120, pattern=r"^[a-z0-9_]+$")
    data_type: Literal["string", "number", "boolean", "date", "enum"] = "string"
    description: str | None = Field(default=None, max_length=2000)
    unit: str | None = Field(default=None, max_length=40)
    is_required: bool = False
    allowed_values: list[str] = Field(default_factory=list, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    category_id: str | None = None


class AttributeDefinitionUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    data_type: Literal["string", "number", "boolean", "date", "enum"] | None = None
    description: str | None = Field(default=None, max_length=2000)
    unit: str | None = Field(default=None, max_length=40)
    is_required: bool | None = None
    allowed_values: list[str] | None = Field(default=None, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=50)
    category_id: str | None = None
    archived: bool | None = None


class BulkIn(Strict):
    operation: str = Field(max_length=40)
    product_ids: list[str] = Field(min_length=1, max_length=5000)
    params: dict = Field(default_factory=dict)
    dry_run: bool = False


class PricingBulkIn(Strict):
    product_ids: list[str] = Field(min_length=1, max_length=5000)
    operation: Literal["price_adjust", "set_sale", "clear_sale", "schedule_price"] = "price_adjust"
    params: dict = Field(default_factory=dict)
    dry_run: bool = True


class InventoryBulkIn(Strict):
    product_ids: list[str] = Field(min_length=1, max_length=5000)
    operation: Literal["set", "increase", "decrease", "mark_out_of_stock"]
    quantity: int = Field(default=0, ge=0, le=1_000_000_000)
    reason: str | None = Field(default=None, max_length=500)
    dry_run: bool = True


class ScheduleSettingsIn(Strict):
    website_id: str | None = None
    frequency: Literal["manual", "daily", "weekly", "monthly"] | None = None
    enabled: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    weekday: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    max_pages: int | None = Field(default=None, ge=1, le=1000)
    max_products: int | None = Field(default=None, ge=1, le=5000)
    extraction_methods: list[Literal["structured_data", "tables", "html", "ai"]] | None = Field(default=None, min_length=1)
    detect: dict[str, bool] | None = None
    conflict_policy: Literal["review", "keep_agentready", "accept_website"] | None = None
    auto_apply: bool | None = None


class SyncStartIn(Strict):
    website_id: str | None = None
    integration_id: str | None = None
    direction: Literal["import", "export"] = "import"


class ResolveIn(Strict):
    conflict_ids: list[str] | None = Field(default=None, max_length=1000)  # None => all open conflicts of the sync
    action: Literal["accept_website", "keep_agentready", "edit", "ignore"]
    value: str | float | int | None = None


class WooConnectIn(Strict):
    store_url: str = Field(min_length=8, max_length=2048)
    consumer_key: str = Field(min_length=10, max_length=200, pattern=r"^ck_[A-Za-z0-9]+$")
    consumer_secret: str = Field(min_length=10, max_length=200, pattern=r"^cs_[A-Za-z0-9]+$")
    name: str | None = Field(default=None, max_length=200)
    inventory_master: Literal["store", "agentready"] = "store"


class ShopifyConnectIn(Strict):
    shop: str = Field(min_length=5, max_length=255)
    access_token: str | None = Field(default=None, min_length=10, max_length=300)  # custom-app Admin API token
    name: str | None = Field(default=None, max_length=200)
    inventory_master: Literal["store", "agentready"] = "store"


class IntegrationSyncIn(Strict):
    direction: Literal["import", "export"] = "import"
    product_ids: list[str] | None = Field(default=None, max_length=5000)
    push_prices: bool = True
    push_inventory: bool = True


class ApiKeyIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[Literal["catalog:read", "pricing:read", "inventory:read", "evidence:read", "agent:read",
                         "checkout:write"]] = Field(
        default_factory=lambda: ["catalog:read", "pricing:read", "inventory:read", "agent:read"], min_length=1)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=10000)
    expires_at: datetime | None = None

    _a = field_validator("expires_at")(lambda cls, v: _aware(v))


class CatalogRulesIn(Strict):
    enforce_unique_skus: bool | None = None
    default_low_stock_threshold: int | None = Field(default=None, ge=0, le=1_000_000_000)
