"""Groq chat-completions integration over plain httpx (no vendor SDK).

API: POST {GROQ_BASE_URL}/chat/completions (OpenAI-compatible wire format).

* Structured Outputs: ``response_format={"type": "json_schema", ...}`` for
  models known to support it (strict mode for models that support strict),
  otherwise JSON mode (``{"type": "json_object"}``). If the API rejects the
  schema format we fall back to JSON mode once. Either way the output is
  validated with Pydantic — JSON mode never guarantees schema correctness.
* Retries: 429 (honours ``retry-after``), 498 (capacity), 5xx and network
  timeouts, with exponential backoff and jitter.
* Model availability: ``verify_model()`` checks ``GET /models`` and is
  called before the first extraction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.services.ai.base import (
    AIClassificationResponse,
    AIConfigurationError,
    AIError,
    AIExtractionResponse,
    AIProvider,
    AIRateLimited,
    AIResponseInvalid,
    AIResult,
    AIUsage,
)
from app.services.ai.prompts import (
    CLASSIFY_SCHEMA,
    CLASSIFY_SYSTEM_PROMPT,
    EXTRACTION_SCHEMA,
    EXTRACTION_SYSTEM_PROMPT,
    json_object_hint,
    wrap_source,
)

log = logging.getLogger("agentready.ai.groq")

# Models documented by Groq as supporting json_schema structured outputs, per
# https://console.groq.com/docs/structured-outputs (checked 2026-11-07). Other models use JSON mode.
JSON_SCHEMA_MODELS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-safeguard-20b",
    "qwen/qwen3.8-27b",
    "meta-models/muse-spark-open",
)
# strict: true (constrained decoding) — the safeguard model is documented as best-effort only.
STRICT_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b", "meta-models/muse-spark-open")
# 400 bodies meaning "this output failed validation" (retry) vs "this format is not supported" (fall back).
GENERATION_FAILED = re.compile(r"json_validate_failed|does not match the expected schema|failed to generate json", re.I)
FORMAT_UNSUPPORTED = re.compile(r"(response_format|json_schema)[^.]{0,80}(not supported|unsupported|invalid|not available)|"
                                r"(not supported|unsupported)[^.]{0,80}(response_format|json_schema|structured output)", re.I)
RETRYABLE_STATUS = {408, 429, 498, 500, 502, 503, 504}


def _error_message(resp: httpx.Response) -> str:
    """Groq's error message without echoing request content back into logs."""
    try:
        err = resp.json().get("error") or {}
        return str(err.get("message") or err.get("code") or resp.reason_phrase)[:300]
    except ValueError:
        return resp.reason_phrase or f"HTTP {resp.status_code}"


def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    return m.group(1) if m else t


class GroqExtractionService(AIProvider):
    name = "groq"
    evidence_method = "ai"

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings or get_settings()
        if not self.s.groq_api_key:
            raise AIConfigurationError("GROQ_API_KEY is not configured")
        self._client = httpx.AsyncClient(
            base_url=self.s.groq_base_url.rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(self.s.groq_timeout_seconds, connect=10.0),
            headers={"Authorization": f"Bearer {self.s.groq_api_key}", "Content-Type": "application/json"},
        )
        self._model_checked: dict | None = None
        self._schema_unsupported = False

    @property
    def model_id(self) -> str:
        return self.s.groq_model

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ models
    async def verify_model(self) -> dict:
        if self._model_checked is not None:
            return self._model_checked
        try:
            resp = await self._client.get("/models")
        except httpx.HTTPError as e:
            return {"available": None, "model": self.model_id, "error": f"could not reach Groq: {type(e).__name__}"}
        if resp.status_code == 401:
            raise AIConfigurationError("Groq rejected the API key (401)")
        if resp.status_code >= 400:
            return {"available": None, "model": self.model_id, "error": f"models endpoint returned {resp.status_code}"}
        ids = [m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)]
        info = {"available": self.model_id in ids, "model": self.model_id, "available_models": sorted(i for i in ids if i)}
        if not info["available"]:
            raise AIConfigurationError(
                f"GROQ_MODEL '{self.model_id}' is not available to this API key. Available: {', '.join(info['available_models'][:15])}"
            )
        self._model_checked = info
        return info

    # ------------------------------------------------------------------ format
    def _response_format(self, schema_name: str, schema: dict) -> tuple[dict, str]:
        mode = self.s.groq_response_format
        if mode == "auto":
            mode = "json_schema" if (self.model_id.startswith(JSON_SCHEMA_MODELS) and not self._schema_unsupported) else "json_object"
        if mode == "json_schema" and not self._schema_unsupported:
            return (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                        "strict": self.model_id.startswith(STRICT_MODELS),
                    },
                },
                "json_schema",
            )
        return {"type": "json_object"}, "json_object"

    def _payload(self, system: str, user: str, schema_name: str, schema: dict) -> tuple[dict, str]:
        rf, rf_name = self._response_format(schema_name, schema)
        if rf_name == "json_object":
            system = system + "\n\n" + json_object_hint(schema_name)
        payload = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": rf,
            "temperature": 0,
            "max_completion_tokens": self.s.groq_max_completion_tokens,
        }
        if self.model_id.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"
        return payload, rf_name

    # ------------------------------------------------------------------ core
    async def _complete(self, system: str, user: str, schema_name: str, schema: dict, model: type[BaseModel]) -> AIResult:
        started = time.monotonic()
        attempts = 0
        last_error: Exception | None = None
        fell_back = False
        while attempts <= self.s.groq_max_retries:
            attempts += 1
            payload, rf_name = self._payload(system, user, schema_name, schema)
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = AIError(f"network error: {type(e).__name__}")
                await self._backoff(attempts)
                continue

            if resp.status_code == 401:
                raise AIConfigurationError("Groq rejected the API key (401). Check GROQ_API_KEY.")
            if resp.status_code == 403:
                raise AIConfigurationError(f"Groq denied access (403): {_error_message(resp)}")
            if resp.status_code == 404:
                raise AIConfigurationError(f"Groq model '{self.model_id}' not found (404)")
            if resp.status_code == 400:
                body = resp.text[:1000]
                if GENERATION_FAILED.search(body):
                    # a single bad generation: retry the same format; never treat it as "format unsupported"
                    last_error = AIResponseInvalid("model output failed Groq's JSON/schema validation")
                    last_error.usage = None  # type: ignore[attr-defined]
                    continue
                if rf_name == "json_schema" and not fell_back and FORMAT_UNSUPPORTED.search(body):
                    log.warning("Groq rejected json_schema for %s; falling back to json_object", self.model_id)
                    self._schema_unsupported = True
                    fell_back = True
                    attempts -= 1
                    continue
                raise AIError(f"Groq rejected the request (400): {_error_message(resp)}")
            if resp.status_code == 413:
                raise AIError("content too large for model context")
            if resp.status_code in RETRYABLE_STATUS:
                last_error = AIRateLimited(f"Groq returned {resp.status_code}")
                await self._backoff(attempts, resp.headers.get("retry-after"))
                continue
            if resp.status_code >= 400:
                raise AIError(f"Groq error {resp.status_code}: {_error_message(resp)}")

            body = resp.json()
            usage = body.get("usage") or {}
            meta = AIUsage(
                provider="groq",
                model=body.get("model") or self.model_id,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
                response_format=rf_name,
            )
            try:
                choice = body["choices"][0]
                content = choice["message"].get("content") or ""
                if choice.get("finish_reason") == "length":
                    raise AIResponseInvalid("model output was truncated (max_completion_tokens reached)")
                parsed = json.loads(_strip_fences(content))
                data = model.model_validate(parsed)
            except AIResponseInvalid as e:
                e.usage = meta  # type: ignore[attr-defined]
                raise
            except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as e:
                err = AIResponseInvalid(f"response failed validation: {type(e).__name__}: {str(e)[:300]}")
                err.usage = meta  # type: ignore[attr-defined]
                raise err from e
            return AIResult(data=data, usage=meta, raw={"id": body.get("id")})
        raise last_error or AIError("Groq request failed")

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(2 ** (attempt - 1), 20) + random.uniform(0, 0.5)
        if retry_after:
            try:
                delay = min(float(retry_after), 60.0)
            except ValueError:
                pass
        await asyncio.sleep(delay)

    async def extract_products(self, content: str, page_url: str) -> AIResult:
        await self.verify_model()
        return await self._complete(
            EXTRACTION_SYSTEM_PROMPT, wrap_source(content, page_url), "product_extraction", EXTRACTION_SCHEMA, AIExtractionResponse
        )

    async def classify_page(self, content: str, page_url: str) -> AIResult:
        await self.verify_model()
        return await self._complete(
            CLASSIFY_SYSTEM_PROMPT, wrap_source(content, page_url), "classify", CLASSIFY_SCHEMA, AIClassificationResponse
        )
