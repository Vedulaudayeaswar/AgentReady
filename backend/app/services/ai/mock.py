"""LOCAL MOCK AI PROVIDER — NOT REAL AI.

Used only when ``AI_PROVIDER=mock`` (tests and offline demos). It applies a
few simple text rules to the prepared content so the AI code path can be
exercised end to end without credentials. Every record it produces is
labelled ``extraction_method="ai_mock"`` and model ``mock-local`` so it can
never be mistaken for real model output.
"""

from __future__ import annotations

import re

from app.services.ai.base import (
    AIAttribute,
    AIClassificationResponse,
    AIEvidence,
    AIExtractionResponse,
    AIPrice,
    AIProduct,
    AIProvider,
    AIResult,
    AIUsage,
)
from app.services.normalization.normalize import detect_currency, find_price_strings, parse_price

MOCK_MODEL = "mock-local"


class MockExtractionProvider(AIProvider):
    name = "mock"
    evidence_method = "ai_mock"

    @property
    def model_id(self) -> str:
        return MOCK_MODEL

    def _usage(self, content: str) -> AIUsage:
        return AIUsage(provider="mock", model=MOCK_MODEL, prompt_tokens=len(content) // 4, completion_tokens=50,
                       total_tokens=len(content) // 4 + 50, response_format="mock")

    async def extract_products(self, content: str, page_url: str) -> AIResult:
        products: list[AIProduct] = []
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        # pipe tables: first row is header
        table_rows = [l for l in lines if l.count("|") >= 1 and not l.startswith("IMAGE")]
        if len(table_rows) >= 2:
            first = [h.strip() for h in table_rows[0].split("|")]
            headerless = any(find_price_strings(c) for c in first)
            header = [f"Column {i + 1}" for i in range(len(first))] if headerless else first
            for row in (table_rows if headerless else table_rows[1:]):
                cells = [c.strip() for c in row.split("|")]
                if len(cells) != len(header) or not cells[0]:
                    continue
                prod = AIProduct(name=cells[0], evidence=[AIEvidence(field="name", source_text=cells[0])])
                for h, c in zip(header[1:], cells[1:]):
                    if find_price_strings(c) or re.search(r"price|rate|mrp|cost", h, re.I):
                        pp = parse_price(c)
                        if pp and pp.amount is not None:
                            cur, _ = detect_currency(c)
                            prod.price = AIPrice(amount=float(pp.amount), currency=cur, source_text=c)
                            continue
                    if c:
                        prod.attributes.append(AIAttribute(name=h or "value", value=c, source_text=c))
                products.append(prod)
        else:
            heading = next((l.lstrip("# ") for l in lines if l.startswith("#")), None)
            if heading:
                prod = AIProduct(name=heading, evidence=[AIEvidence(field="name", source_text=heading)])
                prices = find_price_strings(content)
                if prices:
                    pp = parse_price(prices[0])
                    if pp and pp.amount is not None:
                        prod.price = AIPrice(amount=float(pp.amount), currency=pp.currency if not pp.currency_inferred else None, source_text=prices[0])
                products.append(prod)
        data = AIExtractionResponse(page_contains_products=bool(products), products=products, notes="mock provider output")
        return AIResult(data=data, usage=self._usage(content))

    async def classify_page(self, content: str, page_url: str) -> AIResult:
        prices = find_price_strings(content)
        data = AIClassificationResponse(
            is_product_page=len(prices) == 1, is_listing_page=len(prices) > 1, reason="mock: price-count rule"
        )
        return AIResult(data=data, usage=self._usage(content))
