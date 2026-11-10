"""Provider-independent AI contract.

Nothing outside ``app.services.ai`` may depend on a provider's wire format;
providers return these normalized types.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class AIError(Exception):
    retryable = False


class AIConfigurationError(AIError):
    """Missing key, invalid key, or unavailable model. Not retryable."""


class AIRateLimited(AIError):
    retryable = True


class AIResponseInvalid(AIError):
    """The model returned something that failed JSON/schema validation."""


# ---------------------------------------------------------------- response model
class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AIAttribute(_Strict):
    name: str
    value: str | None = None
    unit: str | None = None
    source_text: str | None = None


class AIKeyValue(_Strict):
    name: str
    value: str | None = None


class AIPrice(_Strict):
    amount: float | None = None
    currency: str | None = None
    source_text: str | None = None


class AIVariant(_Strict):
    name: str | None = None
    sku: str | None = None
    price_amount: float | None = None
    currency: str | None = None
    attributes: list[AIKeyValue] = Field(default_factory=list)


class AIEvidence(_Strict):
    field: str
    source_text: str


class AIProduct(_Strict):
    name: str | None = None
    sku: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    category_is_inferred: bool = False
    description: str | None = None
    price: AIPrice = Field(default_factory=AIPrice)
    availability: str | None = None
    availability_source_text: str | None = None
    product_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    gtin: str | None = None
    mpn: str | None = None
    material: str | None = None
    dimensions: str | None = None
    weight: str | None = None
    attributes: list[AIAttribute] = Field(default_factory=list)
    variants: list[AIVariant] = Field(default_factory=list)
    evidence: list[AIEvidence] = Field(default_factory=list)
    ambiguous_fields: list[str] = Field(default_factory=list)


class AIExtractionResponse(_Strict):
    page_contains_products: bool = False
    products: list[AIProduct] = Field(default_factory=list)
    notes: str | None = None


class AIClassificationResponse(_Strict):
    is_product_page: bool = False
    is_listing_page: bool = False
    reason: str | None = None


# ---------------------------------------------------------------- results
@dataclass
class AIUsage:
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    response_format: str | None = None


@dataclass
class AIResult:
    data: BaseModel
    usage: AIUsage
    raw: dict = field(default_factory=dict)


class AIProvider(abc.ABC):
    name: str = "base"
    #: extraction_method recorded on evidence produced by this provider
    evidence_method: str = "ai"

    @property
    @abc.abstractmethod
    def model_id(self) -> str: ...

    @abc.abstractmethod
    async def extract_products(self, content: str, page_url: str) -> AIResult: ...

    @abc.abstractmethod
    async def classify_page(self, content: str, page_url: str) -> AIResult: ...

    async def verify_model(self) -> dict:
        return {"available": True, "model": self.model_id}

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
