"""Natural-language shopping requests -> structured search filters.

The deterministic parser always runs first (prices, currencies, availability, colours, sizes, voltages, power,
known brands). Groq is used only when the caller explicitly asks for AI interpretation and Groq is configured; it
only produces *filters* — results always come from approved, distributed product records, so the model cannot
invent products, specifications, prices or availability.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings
from app.services.distribution.search import AttrFilter, SearchError, SearchQuery

log = logging.getLogger("agentready.nlquery")
PROMPT_VERSION = "agent-query-v1"
CURRENCY_TOKENS = [("₹", "INR"), ("rs.", "INR"), ("rs", "INR"), ("inr", "INR"), ("rupees", "INR"), ("€", "EUR"),
                   ("eur", "EUR"), ("£", "GBP"), ("gbp", "GBP"), ("usd", "USD"), ("us$", "USD"), ("$", "USD"),
                   ("dollars", "USD")]
COLORS = ("black", "white", "red", "blue", "green", "grey", "gray", "yellow", "pink", "brown", "silver", "gold",
          "orange", "purple", "navy", "beige", "maroon")
SIZES = {"xs": "xs", "extra small": "xs", "small": "s", "medium": "m", "large": "l", "extra large": "xl", "xl": "xl",
         "xxl": "xxl"}
AMOUNT = r"(?P<cur1>₹|rs\.?|inr|€|£|\$|us\$|usd|eur|gbp)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>k|lakh|lac)?\s*(?P<cur2>inr|usd|eur|gbp|rupees|dollars)?"


def _amount(m: re.Match) -> tuple[Decimal | None, str | None]:
    try:
        n = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None, None
    if m.group("k"):
        n *= Decimal(1000) if m.group("k") == "k" else Decimal(100000)
    cur = None
    for tok in (m.group("cur1"), m.group("cur2")):
        if tok:
            cur = dict(CURRENCY_TOKENS).get(tok.lower()) or cur
    return n, cur


def parse(text: str, known_brands: list[str] | None = None) -> tuple[SearchQuery, list[str]]:
    """Returns the query and a list of human-readable notes describing what was understood."""
    t = " " + (text or "").lower().strip() + " "
    q = SearchQuery()
    notes: list[str] = []

    def cut(span):
        nonlocal t
        t = t[:span[0]] + " " + t[span[1]:]

    m = re.search(rf"between\s+{AMOUNT}\s+(?:and|to|-)\s+(?P<rest>.+?)(?=\s|$)", t)
    if m:
        lo, cur = _amount(m)
        m2 = re.match(AMOUNT, m.group("rest"))
        hi, cur2 = _amount(m2) if m2 else (None, None)
        if lo is not None and hi is not None:
            q.min_price, q.max_price = lo, hi
            q.currency = cur or cur2
            notes.append(f"price between {lo} and {hi}")
            cut(m.span())
    for pattern, attr in ((r"(?:under|below|less than|cheaper than|up to|upto|max(?:imum)?|within|<)", "max_price"),
                          (r"(?:over|above|more than|at least|min(?:imum)?|>)", "min_price")):
        m = re.search(rf"{pattern}\s+{AMOUNT}", t)
        if m:
            n, cur = _amount(m)
            if n is not None:
                setattr(q, attr, n)
                q.currency = q.currency or cur
                notes.append(f"{'maximum' if attr == 'max_price' else 'minimum'} price {n}{' ' + cur if cur else ''}")
                cut(m.span())
    if not q.currency:
        for tok, code in CURRENCY_TOKENS:
            if re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", t):
                q.currency = code
                break
    if re.search(r"\b(in stock|available|currently available|ready to ship)\b", t):
        q.availability = ["available"]
        notes.append("only available products")
        t = re.sub(r"\b(in stock|currently available|available|ready to ship)\b", " ", t)
    for c in COLORS:
        if re.search(rf"\b{c}\b", t):
            q.variant_options.append(AttrFilter("color", ":", "grey" if c == "gray" else c))
            notes.append(f"colour {c}")
            t = re.sub(rf"\b{c}\b", " ", t)
            break
    m = re.search(r"\b(?:in\s+)?(?:size\s+(?P<a>xs|s|m|l|xl|xxl|extra small|small|medium|large|extra large)|(?P<b>extra small|small|medium|large|extra large|xxl|xl)\s+size)\b", t)
    if m:
        size = SIZES.get(m.group("a") or m.group("b"), m.group("a") or m.group("b"))
        q.variant_options.append(AttrFilter("size", ":", size))
        notes.append(f"size {size.upper()}")
        cut(m.span())
    for unit_re, key in ((r"(\d+(?:\.\d+)?)\s?(v|volts?|vac|vdc)\b", "voltage"), (r"(\d+(?:\.\d+)?)\s?(kw|w|watts?|hp)\b", "power")):
        m = re.search(unit_re, t)
        if m:
            q.attributes.append(AttrFilter(key, ":", f"{m.group(1)} {m.group(2).upper().replace('VOLTS', 'V').replace('VOLT', 'V').replace('WATTS', 'W').replace('WATT', 'W').replace('KW', 'kW')}"))
            notes.append(f"{key} {m.group(1)}{m.group(2)}")
            cut(m.span())
    for b in sorted(known_brands or [], key=len, reverse=True):
        if b and re.search(rf"(?<![a-z0-9]){re.escape(b.lower())}(?![a-z0-9])", t):
            q.brand = b
            notes.append(f"brand {b}")
            t = re.sub(rf"(?<![a-z0-9]){re.escape(b.lower())}(?![a-z0-9])", " ", t)
            t = re.sub(r"\b(from|by|made by|manufactured by)\b", " ", t)
            break
    t = re.sub(r"[₹$€£]|\b(rs|inr|usd|eur|gbp|rupees|dollars|size|in|from|by|find|show|me|with|rating|rated|"
               r"products?|items?|please|currently|that|are|which)\b", " ", t)
    for f in q.attributes:  # "230V voltage rating": the word "voltage" is now a filter, not a keyword
        t = re.sub(rf"\b{re.escape(f.key)}\b", " ", t)
    q.q = re.sub(r"\s+", " ", t).strip() or None
    if q.q:
        notes.append(f"keywords: {q.q}")
    return q, notes


# ------------------------------------------------------------------ optional Groq interpretation
class _Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=60)
    op: str
    value: str = Field(max_length=100)


class _Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    min_price: float | None = None
    max_price: float | None = None
    currency: str | None = None
    available_only: bool = False
    attributes: list[_Filter] = Field(default_factory=list, max_length=8)
    variant_options: list[_Filter] = Field(default_factory=list, max_length=4)


_NULLABLE = lambda t: {"type": [t, "null"]}  # noqa: E731
_FILTER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["key", "op", "value"],
                  "properties": {"key": {"type": "string"}, "op": {"type": "string", "enum": [":", "=", ">=", "<=", ">", "<"]},
                                 "value": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["keywords", "brand", "category", "min_price", "max_price", "currency", "available_only",
                       "attributes", "variant_options"],
          "properties": {"keywords": _NULLABLE("string"), "brand": _NULLABLE("string"), "category": _NULLABLE("string"),
                         "min_price": _NULLABLE("number"), "max_price": _NULLABLE("number"), "currency": _NULLABLE("string"),
                         "available_only": {"type": "boolean"},
                         "attributes": {"type": "array", "items": _FILTER_SCHEMA},
                         "variant_options": {"type": "array", "items": _FILTER_SCHEMA}}}
SYSTEM = ("You convert a shopper's request into product search filters. Output only filters that the request states "
          "explicitly. Never add product facts, brands, prices or specifications the request does not mention. "
          "currency must be an ISO 4217 code (₹ is INR). attributes are product specifications such as voltage, power, "
          "storage or material, written as key/op/value with the unit in the value (e.g. voltage, :, 230 V). "
          "variant_options are purchasable options such as color and size. keywords are the remaining product words.")


async def interpret_with_groq(text: str, db=None, business_id=None) -> tuple[SearchQuery, dict]:
    """Raises SearchError when Groq is not configured or its output is unusable."""
    s = get_settings()
    if s.ai_provider != "groq" or not s.groq_api_key:
        raise SearchError("AI query interpretation is not available (Groq is not configured)")
    from app.services.ai.groq import GroqExtractionService

    svc = GroqExtractionService(s)
    try:
        res = await svc._complete(SYSTEM, f"Shopping request: {text[:500]}", "search_filters", SCHEMA, _Interpretation)
    finally:
        await svc.aclose()
    data: _Interpretation = res.data  # type: ignore[assignment]
    if db is not None:
        import hashlib

        from app.models import AIRequest
        from app.services.ai.gateway import estimate_cost

        db.add(AIRequest(business_id=business_id, provider="groq", model=res.usage.model, prompt_version=PROMPT_VERSION,
                         purpose="agent_query", content_hash=hashlib.sha256(text.encode()).hexdigest(), status="success",
                         response_format=res.usage.response_format, prompt_tokens=res.usage.prompt_tokens,
                         completion_tokens=res.usage.completion_tokens, total_tokens=res.usage.total_tokens,
                         latency_ms=res.usage.latency_ms, attempts=res.usage.attempts,
                         estimated_cost_usd=estimate_cost(res.usage, s), response_json=data.model_dump()))
    q = SearchQuery(q=data.keywords, brand=data.brand, category=data.category,
                    currency=(data.currency or "").upper() or None,
                    availability=["available"] if data.available_only else [])
    try:
        q.min_price = Decimal(str(data.min_price)) if data.min_price is not None else None
        q.max_price = Decimal(str(data.max_price)) if data.max_price is not None else None
        q.attributes = [AttrFilter.parse(f"{f.key}{f.op}{f.value}") for f in data.attributes]
        q.variant_options = [AttrFilter.parse(f"{f.key}{f.op}{f.value}") for f in data.variant_options]
        q.validate()
    except (SearchError, InvalidOperation, ValidationError) as e:
        raise SearchError(f"The AI interpretation was not a valid search ({e}); use the deterministic interpretation")
    return q, {"source": "groq", "model": res.usage.model, "prompt_version": PROMPT_VERSION,
               "tokens": res.usage.total_tokens}
