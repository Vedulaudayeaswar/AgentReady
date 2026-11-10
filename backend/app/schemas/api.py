"""Request/response models for the REST API."""

from __future__ import annotations

from decimal import Decimal
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------- auth
def check_password_strength(v: str) -> str:
    if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
        raise ValueError("Password must contain letters and numbers")
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)
    business_name: str = Field(min_length=1, max_length=200)

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        return check_password_strength(v)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=10, max_length=256)

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        return check_password_strength(v)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)

    @field_validator("new_password")
    @classmethod
    def strong(cls, v: str) -> str:
        return check_password_strength(v)


class BusinessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    contact_email: str | None
    default_currency: str | None
    country: str | None
    catalog_api_key_prefix: str | None


class MeOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    business: BusinessOut


class BusinessUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    contact_email: EmailStr | None = None
    default_currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")


class AccountUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------- websites / scans
class WebsiteIn(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    name: str | None = Field(default=None, max_length=200)
    max_pages: int = Field(default=50, ge=1, le=1000)
    max_depth: int = Field(default=3, ge=0, le=10)
    use_sitemap: bool = True
    render_js: bool = False


class WebsiteUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    max_pages: int | None = Field(default=None, ge=1, le=1000)
    max_depth: int | None = Field(default=None, ge=0, le=10)
    use_sitemap: bool | None = None
    render_js: bool | None = None


class ScanIn(BaseModel):
    website_id: str
    max_pages: int | None = Field(default=None, ge=1, le=1000)
    max_depth: int | None = Field(default=None, ge=0, le=10)


class ApplyScanIn(BaseModel):
    snapshot_ids: list[str] | None = None  # None => apply all pending changes


class UrlCheckIn(BaseModel):
    url: str = Field(min_length=3, max_length=2048)


# ---------------------------------------------------------------- products
class AttributeIn(BaseModel):
    key: str | None = Field(default=None, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    value: str | None = Field(default=None, max_length=2000)
    unit: str | None = Field(default=None, max_length=40)


class ImageIn(BaseModel):
    url: str = Field(max_length=2048)
    alt: str | None = Field(default=None, max_length=500)


AvailabilityLiteral = Literal[
    "InStock", "OutOfStock", "PreOrder", "BackOrder", "Discontinued", "LimitedAvailability", "SoldOut",
    "OnlineOnly", "InStoreOnly",
]


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=500)
    sku: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    price_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    availability: AvailabilityLiteral | None = None
    product_url: str | None = Field(default=None, max_length=2048)
    gtin: str | None = Field(default=None, pattern=r"^\d{8,14}$")
    mpn: str | None = Field(default=None, max_length=100)
    material: str | None = Field(default=None, max_length=200)
    images: list[ImageIn] | None = None
    attributes: list[AttributeIn] | None = None
    approval_status: Literal["needs_review", "approved", "rejected"] | None = None


class BulkApproveIn(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=1000)
    approval_status: Literal["needs_review", "approved", "rejected"] = "approved"


class ResolveIssueIn(BaseModel):
    action: Literal["resolve", "dismiss", "reopen"] = "resolve"
    note: str | None = Field(default=None, max_length=2000)


class ExportIn(BaseModel):
    format: Literal["json", "csv", "jsonld", "feed"]
    product_ids: list[str] | None = None
    include_unapproved: bool = False
    category: str | None = None
