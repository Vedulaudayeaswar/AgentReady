"""Catalog quality engine (deterministic, explainable).

    product score = 100 x sum(weight_d x score_d) / sum(weight_d)      over the dimensions that APPLY to the product

Each dimension scores 0..1 and lists the exact fields responsible for any shortfall. Dimensions that do not apply
(e.g. variants for a product without variants, distribution while the merchant has distribution switched off) are
left out of the denominator rather than counted as failures. Values the merchant chose to withhold from distribution
(prices, availability, hidden attributes) are reported as *withheld* and never penalised.

The score measures completeness, validity, freshness and provenance — NOT factual accuracy. A populated field is not
proof that it is correct; the source-provenance dimension only records where values came from (verified in the source
page, merchant-entered, or unverified extraction).

Levels: excellent >= 85, good >= 70, needs_improvement >= 50, critical < 50. A product missing a name is always
critical.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.models import AttributeDefinition, Product
from app.services.commerce.catalog_rules import applicable, canonical_key, check_value
from app.services.commerce.money import aware, now_utc
from app.services.distribution.profile import MerchantScope
from app.services.distribution.public import distribution_status, live_variants
from app.services.distribution.units import canonical_unit
from app.services.normalization.normalize import ISO_CODES, SCHEMA_AVAILABILITY, gtin_is_valid

DIMENSIONS = {
    "basic": "Basic information (name, category, brand, manufacturer)",
    "description": "Description quality (present, informative, not repetitive)",
    "images": "Images",
    "identifiers": "Identifiers (SKU, GTIN with valid check digit, MPN)",
    "pricing": "Pricing completeness and validity",
    "category": "Category consistency (assigned to a managed category)",
    "attributes": "Attribute completeness (category-specific required attributes, valid values)",
    "variants": "Variant completeness (SKU and options for every live variant)",
    "freshness": "Information freshness (product, price and stock updates)",
    "distribution": "Distribution readiness",
    "structured_data": "Structured-data validity (GTIN, availability, currency, URLs, validation errors)",
    "provenance": "Source provenance (verified in source page or merchant-entered vs unverified extraction)",
}
DEFAULT_WEIGHTS = {"basic": 10, "description": 10, "images": 10, "identifiers": 8, "pricing": 12, "category": 6,
                   "attributes": 12, "variants": 5, "freshness": 8, "distribution": 8, "structured_data": 6, "provenance": 5}
LEVELS = (("excellent", 85), ("good", 70), ("needs_improvement", 50), ("critical", 0))
KEY_FIELDS = ("name", "price", "description", "brand", "gtin", "sku")


def level_for(score: float) -> str:
    return next(name for name, low in LEVELS if score >= low)


@dataclass
class QualityContext:
    scope: MerchantScope
    definitions: list[AttributeDefinition]
    ancestors: dict[uuid.UUID, set[uuid.UUID]]
    category_requirements: dict[str, list[str]]
    evidence: dict[uuid.UUID, dict[str, str]]  # product -> field -> provenance (verified|merchant|deterministic|ai)
    open_errors: dict[uuid.UUID, int]
    last_price_change: dict[uuid.UUID, datetime]
    last_stock_change: dict[uuid.UUID, datetime]
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    stale_after_days: int = 90
    stale_price_days: int = 180
    stale_stock_days: int = 30
    now: datetime = field(default_factory=now_utc)


@dataclass
class Dim:
    score: float | None  # None = not applicable
    issues: list[str] = field(default_factory=list)


def _days(dt: datetime | None, now: datetime) -> float | None:
    return (now - aware(dt)).total_seconds() / 86400 if dt else None


def _sentences(text: str) -> list[str]:
    return [s.strip().lower() for s in re.split(r"[.!?\n]+", text) if len(s.strip()) > 12]


def required_attribute_keys(p: Product, ctx: QualityContext) -> tuple[list[str], list[AttributeDefinition]]:
    cats = ctx.ancestors.get(p.category_id, set()) if p.category_id else set()
    defs = applicable(ctx.definitions, cats)
    keys = [d.key for d in defs if d.is_required]
    for cid in cats:
        keys += [k for k in ctx.category_requirements.get(str(cid), []) if k not in keys]
    return keys, defs


def evaluate(p: Product, ctx: QualityContext) -> dict:
    now = ctx.now
    missing_req: list[str] = []
    missing_opt: list[str] = []
    invalid: list[str] = []
    stale: list[str] = []
    withheld: list[str] = []
    d: dict[str, Dim] = {}

    # basic
    issues = []
    if not (p.name or "").strip():
        missing_req.append("name")
        issues.append("name is missing")
    if not p.category and not p.category_id:
        missing_req.append("category")
        issues.append("category is missing")
    for f in ("brand", "manufacturer"):
        if not getattr(p, f):
            missing_opt.append(f)
            issues.append(f"{f} is missing")
    d["basic"] = Dim(max(0.0, 1 - 0.4 * (("name" in missing_req) + ("category" in missing_req))
                        - 0.1 * (("brand" in missing_opt) + ("manufacturer" in missing_opt))), issues)

    # description
    desc = (p.public_description or p.description or "").strip()
    if not desc:
        missing_req.append("description")
        d["description"] = Dim(0.0, ["description is missing"])
    else:
        score, issues = 1.0, []
        if len(desc) < 60:
            score, issues = 0.4, [f"description is very short ({len(desc)} characters)"]
        elif len(desc) < 200:
            score, issues = 0.75, [f"description is short ({len(desc)} characters; 200+ recommended)"]
        if p.name and desc.lower() == p.name.strip().lower():
            score, issues = 0.2, ["description only repeats the product name"]
        sents = _sentences(desc)
        if len(sents) >= 2 and len(set(sents)) < len(sents):
            score -= 0.2
            issues.append(f"description repeats {len(sents) - len(set(sents))} sentence(s)")
        d["description"] = Dim(max(score, 0.0), issues)

    # images
    imgs = [i for i in (p.public_images if p.public_images is not None else [x.get("url") for x in p.images or []]) if i]
    if not imgs:
        missing_req.append("images")
    elif len(imgs) == 1:
        missing_opt.append("additional_images")
    d["images"] = Dim(0.0 if not imgs else 0.7 if len(imgs) == 1 else 1.0,
                      ["no images"] if not imgs else ["only one image"] if len(imgs) == 1 else [])

    # identifiers
    issues = []
    score = 0.0
    if p.gtin and gtin_is_valid(p.gtin):
        score += 0.6
    elif p.gtin:
        invalid.append("gtin")
        issues.append(f"GTIN {p.gtin} has an invalid check digit")
    else:
        missing_opt.append("gtin")
        issues.append("no GTIN/barcode")
    if p.sku:
        score += 0.3
    else:
        missing_opt.append("sku")
        issues.append("no SKU")
    if p.mpn:
        score += 0.1
    else:
        missing_opt.append("mpn")
    d["identifiers"] = Dim(min(score, 1.0), issues)

    # pricing
    issues = []
    has_variant_price = any(v.price_amount is not None for v in live_variants(p))
    if p.price_amount is None and not has_variant_price:
        missing_req.append("price")
        d["pricing"] = Dim(0.0, ["no price"])
    else:
        score = 1.0
        if not p.currency:
            invalid.append("currency")
            missing_req.append("currency")
            score, issues = 0.3, ["price has no currency"]
        if p.sale_price_amount is not None and p.price_amount is not None and p.sale_price_amount >= p.price_amount:
            invalid.append("sale_price")
            score -= 0.3
            issues.append("sale price is not lower than the regular price")
        if p.sale_ends_at and p.sale_price_amount is not None and aware(p.sale_ends_at) < now:
            issues.append("an expired sale price is still stored")
        d["pricing"] = Dim(max(score, 0.0), issues)
    if not ctx.scope.profile.show_prices and ctx.scope.profile.enabled:
        withheld.append("price (not shared with agents — not penalised)")

    # category
    if p.category_id:
        d["category"] = Dim(1.0)
    elif p.category:
        d["category"] = Dim(0.5, [f"category '{p.category}' is text only (not mapped to a managed category)"
                                  + (" and was inferred" if p.category_inferred else "")])
    else:
        d["category"] = Dim(0.0, ["no category"])

    # attributes
    required, defs = required_attribute_keys(p, ctx)
    have = {canonical_key(a.key): a for a in p.attributes if a.value not in (None, "")}
    issues = []
    if required:
        miss = [k for k in required if canonical_key(k) not in have]
        missing_req += [f"attribute:{k}" for k in miss]
        if miss:
            issues.append(f"missing required attribute(s): {', '.join(miss)}")
        score = 1 - len(miss) / len(required)
    else:
        n = len(have)
        score = 1.0 if n >= 3 else 0.6 if n >= 1 else 0.0
        if n < 3:
            issues.append(f"{n} attribute(s); no required attributes are defined for this category"
                          if n else "no attributes")
            missing_opt.append("attributes")
    by_key = {d_.key: d_ for d_ in defs}
    for k, a in have.items():
        dd = by_key.get(k)
        problem = check_value(dd, a.value) if dd else None
        if dd and dd.unit and a.unit and canonical_unit(a.unit) != canonical_unit(dd.unit):
            problem = problem or f"unit '{a.unit}' differs from the defined unit '{dd.unit}'"
        if problem:
            invalid.append(f"attribute:{k}")
            issues.append(f"{a.label}: {problem}")
            score -= 0.15
    d["attributes"] = Dim(max(score, 0.0), issues)

    # variants
    vs = live_variants(p)
    if vs:
        bad = [v for v in vs if not v.sku or not (v.attributes or {})]
        keysets = {tuple(sorted((v.attributes or {}).keys())) for v in vs}
        issues = [f"{len(bad)} variant(s) without SKU or options"] if bad else []
        score = 1 - len(bad) / len(vs)
        if len(keysets) > 1:
            invalid.append("variant_options")
            issues.append("variants use different option names")
            score -= 0.2
        d["variants"] = Dim(max(score, 0.0), issues)
    else:
        d["variants"] = Dim(None)

    # freshness
    issues = []
    age = _days(max(filter(None, [aware(p.updated_at), *(aware(v.updated_at) for v in vs if v.updated_at)])), now) \
        if p.updated_at else None
    score = 1.0
    if age is not None and age > ctx.stale_after_days:
        stale.append("product")
        issues.append(f"not updated for {int(age)} days")
        score = 0.3
    elif age is not None and age > ctx.stale_after_days / 3:
        score = 0.7
    price_age = _days(ctx.last_price_change.get(p.id) or p.created_at, now)
    if p.price_amount is not None and price_age is not None and price_age > ctx.stale_price_days:
        stale.append("price")
        issues.append(f"price unchanged and unverified for {int(price_age)} days")
        score -= 0.2
    if p.track_inventory:
        stock_age = _days(ctx.last_stock_change.get(p.id) or p.created_at, now)
        if stock_age is not None and stock_age > ctx.stale_stock_days:
            stale.append("stock")
            issues.append(f"stock not counted or adjusted for {int(stock_age)} days")
            score -= 0.2
    d["freshness"] = Dim(max(score, 0.0), issues)

    # distribution readiness
    st = distribution_status(p, ctx.scope)
    blockers = [r for r in st["reasons"] if "distribution is disabled" not in r.lower() and "expired" not in r]
    if not ctx.scope.profile.enabled:
        d["distribution"] = Dim(None)  # merchant-level choice: not counted against products
        if p.approval_status != "approved" or p.publication_status != "published":
            blockers = [r for r in st["reasons"] if "Public distribution" not in r]
    else:
        d["distribution"] = Dim(1.0 if st["distributed"] else 0.5 if st["eligible"] else 0.0, blockers)
    for k in ctx.scope.profile.hidden_attribute_keys or []:
        if canonical_key(k) in have:
            withheld.append(f"attribute:{k} (hidden from agents — not penalised)")

    # structured data validity
    checks, issues = 0, []
    total = 4
    if p.gtin and not gtin_is_valid(p.gtin):
        issues.append("invalid GTIN")
    else:
        checks += 1
    if p.availability and p.availability.lower() not in SCHEMA_AVAILABILITY:
        issues.append(f"availability '{p.availability}' is not a schema.org value")
        invalid.append("availability")
    else:
        checks += 1
    if p.currency and p.currency.upper() not in ISO_CODES and not re.fullmatch(r"[A-Z]{3}", p.currency):
        issues.append(f"currency '{p.currency}' is not an ISO code")
    else:
        checks += 1
    url = p.product_url or p.source_url
    if url and not url.startswith(("http://", "https://")):
        issues.append("product URL is not http(s)")
    else:
        checks += 1
    errors = ctx.open_errors.get(p.id, 0)
    score = checks / total - min(0.5, 0.15 * errors)
    if errors:
        issues.append(f"{errors} open validation error(s)")
    d["structured_data"] = Dim(max(score, 0.0), issues)

    # provenance
    prov = ctx.evidence.get(p.id, {})
    weights = {"verified": 1.0, "merchant": 0.9, "deterministic": 0.7, "ai": 0.4}
    present = [f for f in KEY_FIELDS if (getattr(p, "price_amount" if f == "price" else f, None))]
    if present:
        vals = [weights.get(prov.get(f, ""), 0.5) for f in present]
        unverified = [f for f in present if prov.get(f) in ("ai", None)]
        d["provenance"] = Dim(sum(vals) / len(vals),
                              [f"{', '.join(unverified)}: no verified source (AI-extracted or no evidence)"] if unverified else [])
    else:
        d["provenance"] = Dim(None)

    w = {k: float(ctx.weights.get(k, DEFAULT_WEIGHTS[k])) for k in DEFAULT_WEIGHTS}
    applicable_dims = {k: v for k, v in d.items() if v.score is not None and w[k] > 0}
    denom = sum(w[k] for k in applicable_dims) or 1.0
    score = round(100 * sum(w[k] * v.score for k, v in applicable_dims.items()) / denom, 1)
    level = "critical" if "name" in missing_req else level_for(score)
    suggestions = suggestions_for(missing_req, missing_opt, invalid, stale, blockers, d)
    return {
        "score": score, "level": level,
        "dimensions": {k: {"score": None if v.score is None else round(100 * v.score, 1), "weight": w[k],
                           "label": DIMENSIONS[k], "issues": v.issues, "applies": v.score is not None}
                       for k, v in d.items()},
        "missing_required": missing_req, "missing_optional": missing_opt, "invalid": invalid, "stale": stale,
        "blockers": blockers, "withheld": withheld, "suggestions": suggestions,
    }


def suggestions_for(missing_req, missing_opt, invalid, stale, blockers, dims) -> list[str]:
    out = []
    for f in missing_req:
        if f.startswith("attribute:"):
            out.append(f"Add the {f.split(':', 1)[1]} specification from a verified source (datasheet, packaging)")
        else:
            out.append(f"Add the missing {f}")
    if "gtin" in invalid:
        out.append("Correct the GTIN (its check digit is invalid) or remove it")
    if "sale_price" in invalid:
        out.append("Fix the sale price: it must be lower than the regular price")
    if dims["description"].score is not None and 0 < dims["description"].score < 1:
        out.append("Expand the description with facts you can verify; the AI assistant can restructure existing text")
    if "images" in missing_req or "additional_images" in missing_opt:
        out.append("Add product images (several angles help agents and shoppers)")
    if "gtin" in missing_opt:
        out.append("Add a GTIN/barcode if the product has one (never invent one)")
    for s in stale:
        out.append({"product": "Review and confirm this product's information",
                    "price": "Confirm the current price", "stock": "Count stock or adjust inventory"}[s])
    out += [f"Distribution blocker: {b}" for b in blockers]
    return list(dict.fromkeys(out))[:12]
