"""Canonical AgentReady product schema (internal, provider-independent).

Modelled on schema.org Product/Offer. Every extractor (JSON-LD, microdata,
HTML heuristics, tables, AI) produces ``ExtractedProduct`` records; the
normalizer, validator and persistence layers only ever see this shape.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal[
    "json_ld", "microdata", "meta", "html", "html_table", "html_spec", "ai", "merchant_edit"
]
ExtractionMethod = Literal["deterministic", "ai", "ai_mock", "merchant"]

AVAILABILITY_VALUES = (
    "InStock",
    "OutOfStock",
    "PreOrder",
    "BackOrder",
    "Discontinued",
    "LimitedAvailability",
    "SoldOut",
    "OnlineOnly",
    "InStoreOnly",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProductPrice(_Model):
    amount: Decimal | None = None
    currency: str | None = None
    raw: str | None = None
    currency_inferred: bool = False


class ProductImage(_Model):
    url: str
    alt: str | None = None


class ProductAttribute(_Model):
    key: str
    label: str
    value: str | None = None
    unit: str | None = None
    raw_value: str | None = None


class ProductVariant(_Model):
    name: str | None = None
    sku: str | None = None
    price: ProductPrice | None = None
    availability: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)


class ProductEvidence(_Model):
    field: str
    value: Any = None
    source_value: str | None = None
    source_url: str | None = None
    source_fragment: str | None = None
    source_type: SourceType
    extraction_method: ExtractionMethod = "deterministic"
    inferred: bool = False
    model_id: str | None = None
    prompt_version: str | None = None
    confidence: float | None = None
    verified_in_source: bool | None = None


class Measurement(_Model):
    value: str
    unit: str | None = None
    raw: str | None = None


class ExtractedProduct(_Model):
    name: str | None = None
    sku: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    category_inferred: bool = False
    description: str | None = None
    price: ProductPrice | None = None
    availability: str | None = None
    product_url: str | None = None
    source_url: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    material: str | None = None
    dimensions: Measurement | None = None
    weight: Measurement | None = None
    images: list[ProductImage] = Field(default_factory=list)
    attributes: list[ProductAttribute] = Field(default_factory=list)
    variants: list[ProductVariant] = Field(default_factory=list)
    evidence: list[ProductEvidence] = Field(default_factory=list)
    # Flags raised during extraction (e.g. conflicting sources, ambiguity).
    flags: list[dict[str, Any]] = Field(default_factory=list)
    origin: str = "html"  # which extractor produced the record

    def evidence_for(self, field: str) -> list[ProductEvidence]:
        return [e for e in self.evidence if e.field == field]


class ExtractionResult(_Model):
    page_url: str
    page_type: str
    products: list[ExtractedProduct] = Field(default_factory=list)
    used_ai: bool = False
    ai_model: str | None = None
    notes: list[str] = Field(default_factory=list)
