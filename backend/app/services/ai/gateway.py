"""AI gateway: the only entry point the rest of the app uses for AI.

Adds, on top of a provider: per-scan call budget, response caching keyed by
(content hash, model, prompt version, business), request logging with token
usage and estimated cost, and graceful degradation (errors are recorded and
the scan continues with deterministic results).
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import AIRequest
from app.services.ai.base import (
    AIClassificationResponse,
    AIConfigurationError,
    AIError,
    AIExtractionResponse,
    AIProvider,
    AIUsage,
)
from app.services.ai.prompts import CLASSIFY_PROMPT_VERSION, EXTRACTION_PROMPT_VERSION

log = logging.getLogger("agentready.ai")


def build_provider(settings: Settings | None = None, transport=None) -> AIProvider | None:
    s = settings or get_settings()
    if s.ai_provider == "mock":
        from app.services.ai.mock import MockExtractionProvider

        return MockExtractionProvider()
    if s.ai_provider == "groq" and s.groq_api_key:
        from app.services.ai.groq import GroqExtractionService

        return GroqExtractionService(s, transport=transport)
    return None


def estimate_cost(usage: AIUsage, s: Settings) -> float:
    if usage.provider != "groq":
        return 0.0
    return round(
        usage.prompt_tokens / 1e6 * s.groq_input_price_per_mtok + usage.completion_tokens / 1e6 * s.groq_output_price_per_mtok,
        6,
    )


class AIGateway:
    def __init__(
        self,
        provider: AIProvider,
        session: AsyncSession,
        business_id: uuid.UUID,
        scan_id: uuid.UUID | None = None,
        settings: Settings | None = None,
    ):
        self.provider = provider
        self.session = session
        self.business_id = business_id
        self.scan_id = scan_id
        self.s = settings or get_settings()
        self.calls = 0
        self.disabled_reason: str | None = None

    @property
    def model_id(self) -> str:
        return self.provider.model_id

    @property
    def method(self) -> str:
        return self.provider.evidence_method

    @property
    def available(self) -> bool:
        return self.disabled_reason is None and self.calls < self.s.max_ai_calls_per_scan

    async def _cached(self, h: str, version: str) -> dict | None:
        row = (
            await self.session.execute(
                select(AIRequest)
                .where(
                    AIRequest.business_id == self.business_id,
                    AIRequest.content_hash == h,
                    AIRequest.model == self.model_id,
                    AIRequest.prompt_version == version,
                    AIRequest.status == "success",
                )
                .order_by(AIRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row.response_json if row else None

    async def _log(self, purpose: str, h: str, version: str, status: str, usage: AIUsage | None,
                   response: dict | None = None, error: str | None = None) -> None:
        self.session.add(
            AIRequest(
                business_id=self.business_id,
                scan_id=self.scan_id,
                provider=self.provider.name,
                model=self.model_id,
                prompt_version=version,
                purpose=purpose,
                content_hash=h,
                status=status,
                response_format=usage.response_format if usage else None,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
                latency_ms=usage.latency_ms if usage else 0,
                attempts=usage.attempts if usage else 0,
                estimated_cost_usd=estimate_cost(usage, self.s) if usage else 0.0,
                error=error[:2000] if error else None,
                response_json=response,
            )
        )
        await self.session.flush()

    async def _run(self, purpose: str, version: str, content: str, page_url: str, model_cls, call):
        h = hashlib.sha256(f"{purpose}\n{content}".encode()).hexdigest()
        cached = await self._cached(h, version)
        if cached is not None:
            await self._log(purpose, h, version, "cached", None, None)
            return model_cls.model_validate(cached)
        if not self.available:
            return None
        self.calls += 1
        try:
            result = await call(content, page_url)
        except AIConfigurationError as e:
            self.disabled_reason = str(e)
            await self._log(purpose, h, version, "error", None, error=str(e))
            log.error("AI disabled for this scan: %s", e)
            return None
        except AIError as e:
            await self._log(purpose, h, version, "error", getattr(e, "usage", None), error=str(e))
            log.warning("AI %s failed for %s: %s", purpose, page_url, e)
            return None
        data = result.data.model_dump(mode="json")
        await self._log(purpose, h, version, "success", result.usage, data)
        return result.data

    async def extract(self, content: str, page_url: str) -> AIExtractionResponse | None:
        return await self._run("extract", EXTRACTION_PROMPT_VERSION, content, page_url, AIExtractionResponse,
                               self.provider.extract_products)

    async def classify(self, content: str, page_url: str) -> AIClassificationResponse | None:
        return await self._run("classify", CLASSIFY_PROMPT_VERSION, content, page_url, AIClassificationResponse,
                               self.provider.classify_page)
