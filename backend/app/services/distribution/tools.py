"""Typed, read-only agent tools. One registry serves the MCP server and the REST tool endpoint.

Each tool: name, description, typed input (Pydantic, extra fields rejected), a JSON output schema, an
authorization rule (all tools are read-only and see only distributed products), and structured errors.
Privileged merchant actions (pricing, inventory, publication) are deliberately NOT exposed as tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.distribution import agent
from app.services.distribution.agent import AgentContext, AgentError
from app.services.distribution.search import AttrFilter, SearchError, SearchQuery

Availability = Literal["available", "out_of_stock", "preorder", "backorder", "discontinued", "unknown"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchProductsIn(_In):
    query: str | None = Field(None, max_length=200, description="Keywords: product name, type, brand, SKU, GTIN or attribute words")
    brand: str | None = Field(None, max_length=200)
    sku: str | None = Field(None, max_length=200, description="Exact SKU")
    gtin: str | None = Field(None, max_length=20, description="Exact GTIN/EAN/UPC barcode")
    category: str | None = Field(None, max_length=300, description="Category id or part of a category path")
    min_price: Decimal | None = Field(None, ge=0)
    max_price: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, pattern="^[A-Z]{3}$", description="ISO 4217 code; required for price filters when merchants use several currencies")
    availability: list[Availability] = Field(default_factory=list)
    merchant: str | None = Field(None, max_length=80, description="Merchant id (slug) from search_merchants")
    attributes: list[str] = Field(default_factory=list, max_length=10,
                                  description="Attribute filters such as 'voltage:230 V', 'storage>=512 GB', 'material:steel'")
    variant_options: list[str] = Field(default_factory=list, max_length=5,
                                       description="Variant filters such as 'color:black', 'size:M'")
    sort: Literal["relevance", "price_asc", "price_desc", "name", "updated"] = "relevance"
    page: int = Field(1, ge=1, le=1000)
    page_size: int = Field(10, ge=1, le=50)


class ProductIdIn(_In):
    product_id: str = Field(min_length=1, max_length=64, description="Product id from search results")


class CompareIn(_In):
    product_ids: list[str] = Field(min_length=2, max_length=10, description="2-10 product ids (may be from different merchants)")


class MerchantsIn(_In):
    query: str | None = Field(None, max_length=200, description="Filter by merchant name")


class MerchantCatalogIn(_In):
    merchant: str = Field(min_length=1, max_length=80, description="Merchant id (slug)")
    page: int = Field(1, ge=1, le=1000)
    page_size: int = Field(20, ge=1, le=50)


class RecommendIn(_In):
    request: str = Field(min_length=2, max_length=500, description="What the shopper is looking for, in plain language")
    max_price: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    available_only: bool = True
    category: str | None = Field(None, max_length=300)
    limit: int = Field(5, ge=1, le=20)


# ------------------------------------------------------------------ output schemas
_S = {"type": "string"}
_NS = {"type": ["string", "null"]}
PRICE = {"type": ["object", "null"], "properties": {"currency": _NS, "regular": _NS, "current": _S, "sale": _NS,
                                                     "on_sale": {"type": "boolean"}, "valid_until": _NS}}
AVAIL = {"type": ["object", "null"], "properties": {"status": {"enum": list(Availability.__args__)}, "source": _NS}}
VARIANT = {"type": "object", "required": ["id", "options"],
           "properties": {"id": _S, "name": _NS, "sku": _NS, "options": {"type": "object"}, "price": PRICE, "availability": AVAIL}}
PRODUCT = {"type": "object", "required": ["id", "merchant", "name", "price", "availability", "variants", "attributes",
                                          "unknown_fields", "withheld_fields", "updated_at"],
           "properties": {"id": _S, "merchant": {"type": "object"}, "name": _NS, "description": _NS, "brand": _NS,
                          "identifiers": {"type": "object"}, "category": {"type": ["object", "null"]}, "url": _NS,
                          "images": {"type": "array", "items": _S}, "price": PRICE, "availability": AVAIL,
                          "attributes": {"type": "array", "items": {"type": "object"}},
                          "variants": {"type": "array", "items": VARIANT},
                          "unknown_fields": {"type": "array", "items": _S}, "withheld_fields": {"type": "array", "items": _S},
                          "updated_at": _NS}}
SEARCH_OUT = {"type": "object", "required": ["items", "total", "page", "page_size", "has_more", "ranking_policy"],
              "properties": {"items": {"type": "array", "items": {"type": "object", "required": ["product", "ranking"],
                                                                   "properties": {"product": PRODUCT, "ranking": {"type": "object"},
                                                                                  "matched_variant_ids": {"type": "array"}}}},
                             "total": {"type": "integer"}, "page": {"type": "integer"}, "page_size": {"type": "integer"},
                             "has_more": {"type": "boolean"}, "query": {"type": "object"},
                             "ranking_policy": {"type": "object"}, "as_of": _S}}
DETAIL_OUT = {"type": "object", "required": ["product"], "properties": {"product": PRODUCT}}
VARIANTS_OUT = {"type": "object", "required": ["product_id", "variants"],
                "properties": {"product_id": _S, "variants": {"type": "array", "items": VARIANT}, "has_variants": {"type": "boolean"}}}
AVAIL_OUT = {"type": "object", "required": ["product_id", "availability", "variants"],
             "properties": {"product_id": _S, "availability": AVAIL, "variants": {"type": "array"}}}
PRICING_OUT = {"type": "object", "required": ["product_id", "price", "price_visibility", "variants"],
               "properties": {"product_id": _S, "price": PRICE, "price_visibility": {"enum": ["public", "unknown", "withheld"]},
                              "variants": {"type": "array"}}}
COMPARE_OUT = {"type": "object", "required": ["comparison"],
               "properties": {"comparison": {"type": "object", "required": ["products", "rows"],
                                             "properties": {"products": {"type": "array"}, "rows": {"type": "array"}}}}}
MERCHANTS_OUT = {"type": "object", "required": ["items", "total"],
                 "properties": {"items": {"type": "array", "items": {"type": "object", "required": ["id", "name"],
                                                                      "properties": {"id": _S, "name": _NS, "product_count": {"type": "integer"}}}},
                                "total": {"type": "integer"}}}
RECOMMEND_OUT = {**SEARCH_OUT, "required": SEARCH_OUT["required"] + ["interpretation"],
                 "properties": {**SEARCH_OUT["properties"], "interpretation": {"type": "object"}}}


def _query(i: SearchProductsIn) -> SearchQuery:
    return SearchQuery(q=i.query, brand=i.brand, sku=i.sku, gtin=i.gtin, category=i.category, min_price=i.min_price,
                       max_price=i.max_price, currency=i.currency, availability=list(i.availability),
                       merchants=[i.merchant] if i.merchant else [],
                       attributes=[AttrFilter.parse(a) for a in i.attributes],
                       variant_options=[AttrFilter.parse(a) for a in i.variant_options],
                       sort=i.sort, page=i.page, page_size=i.page_size)


async def _search(db, actx, i: SearchProductsIn):
    return await agent.search(db, actx, _query(i))


async def _recommend(db, actx, i: RecommendIn):
    base = SearchQuery(max_price=i.max_price, currency=i.currency, category=i.category,
                       availability=["available"] if i.available_only else [])
    return await agent.recommend(db, actx, i.request, base, "deterministic", i.limit)


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    output_schema: dict
    run: Callable[[Any, AgentContext, Any], Awaitable[dict]]

    def definition(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "title": self.title, "description": self.description, "inputSchema": schema,
                "outputSchema": self.output_schema,
                "annotations": {"title": self.title, "readOnlyHint": True, "destructiveHint": False,
                                "idempotentHint": True, "openWorldHint": False}}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_products", "Search products",
         "Search approved, merchant-distributed products by keywords, brand, SKU, GTIN, category, price range (in one "
         "currency), availability, merchant, attributes (e.g. 'voltage:230 V') and variant options (e.g. 'size:M'). "
         "Results include transparent ranking signals. Never returns private merchant data.",
         SearchProductsIn, SEARCH_OUT, _search),
    Tool("get_product_details", "Get product details",
         "Full public details of one product: description, brand, identifiers, category, images, attributes, "
         "specifications, variants, current price and availability. Unknown values are listed in unknown_fields.",
         ProductIdIn, DETAIL_OUT, lambda db, a, i: agent.product_detail(db, a, i.product_id)),
    Tool("compare_products", "Compare products",
         "Compare 2-10 products (also across merchants): price, currency, brand, availability, shipping, variants and "
         "attributes. Units are converted only with explicit rules; different currencies are never converted; missing "
         "values are reported as unknown.",
         CompareIn, COMPARE_OUT, lambda db, a, i: agent.compare(db, a, i.product_ids)),
    Tool("get_product_variants", "Get product variants",
         "List the purchasable variants of a product with their options (size, colour, ...), price and availability.",
         ProductIdIn, VARIANTS_OUT, lambda db, a, i: agent.variants(db, a, i.product_id)),
    Tool("check_product_availability", "Check availability",
         "Current availability of a product and its variants: available, out_of_stock, preorder, backorder, "
         "discontinued or unknown. Stock quantities are never disclosed.",
         ProductIdIn, AVAIL_OUT, lambda db, a, i: agent.availability(db, a, i.product_id)),
    Tool("get_product_pricing", "Get product pricing",
         "Current regular and sale price in the merchant's currency, sale dates and when the price is next due to "
         "change. Cost prices are never disclosed.",
         ProductIdIn, PRICING_OUT, lambda db, a, i: agent.pricing(db, a, i.product_id)),
    Tool("search_merchants", "Search merchants",
         "List merchants whose catalogs you can access, with product counts, currencies and policy links.",
         MerchantsIn, MERCHANTS_OUT, lambda db, a, i: agent.merchants(db, a, i.query)),
    Tool("get_merchant_catalog", "Get merchant catalog",
         "Page through one merchant's distributed catalog.",
         MerchantCatalogIn, SEARCH_OUT, lambda db, a, i: agent.merchant_catalog(db, a, i.merchant, i.page, i.page_size)),
    Tool("recommend_products", "Recommend products",
         "Match a plain-language request (e.g. 'black T-shirts in medium size under 500 INR') to real products. The "
         "request is interpreted into filters deterministically; results always come from approved product records.",
         RecommendIn, RECOMMEND_OUT, _recommend),
]}


class ToolInputError(ValueError):
    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.details = details


async def call(db, actx: AgentContext, name: str, arguments: dict | None) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        raise KeyError(name)
    try:
        args = tool.input_model.model_validate(arguments or {})
    except ValidationError as e:
        raise ToolInputError("Invalid arguments: " + "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'input'}: {err['msg']}" for err in e.errors()[:5]),
            [{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()])
    try:
        return await tool.run(db, actx, args)
    except SearchError as e:
        raise ToolInputError(str(e))


__all__ = ["TOOLS", "call", "ToolInputError", "AgentError"]
