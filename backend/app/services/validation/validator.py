"""Validation engine: rules -> issues -> quality status and completeness score."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, ValidationIssue
from app.schemas.canonical import AVAILABILITY_VALUES
from app.services.deduplication.dedupe import find_duplicates
from app.services.normalization.normalize import gtin_is_valid

MEASUREMENT_KEYS = re.compile(r"(voltage|volt|current|power|watt|rpm|speed|weight|length|width|height|depth|diameter|"
                              r"capacity|torque|pressure|temperature|frequency)", re.I)

FLAG_RULES = {
    "conflicting_values": ("warning", "Sources disagree"),
    "unverified_ai_evidence": ("warning", "AI value not verified in source"),
    "ambiguous_field": ("warning", "Ambiguous value"),
    "ai_value_dropped": ("info", "AI value discarded"),
    "multiple_offers": ("info", "Multiple offers"),
    "price_from_variants": ("info", "Price from variants"),
    "currency_from_default": ("info", "Default currency applied"),
}

COMPLETENESS_WEIGHTS = {
    "name": 20, "price": 20, "currency": 10, "identifier": 10, "category": 10,
    "description": 10, "images": 10, "availability": 5, "brand": 5,
}


@dataclass
class IssueSpec:
    code: str
    severity: str
    field: str | None
    message: str
    details: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.code, self.field)


def _valid_http(u: str | None) -> bool:
    if not u:
        return False
    try:
        p = urlsplit(u)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def completeness(p: Product) -> float:
    have = {
        "name": bool(p.name),
        "price": p.price_amount is not None,
        "currency": bool(p.currency),
        "identifier": bool(p.sku or p.gtin or p.mpn),
        "category": bool(p.category),
        "description": bool(p.description),
        "images": bool(p.images),
        "availability": bool(p.availability),
        "brand": bool(p.brand),
    }
    return float(sum(w for k, w in COMPLETENESS_WEIGHTS.items() if have[k]))


def validate_product(p: Product, definitions: list | None = None) -> list[IssueSpec]:
    out: list[IssueSpec] = []
    add = lambda *a, **k: out.append(IssueSpec(*a, **k))  # noqa: E731

    name = (p.name or "").strip()
    if not name:
        add("missing_name", "error", "name", "Product has no name")
    elif len(name) < 3 or re.fullmatch(r"[\d\W_]+", name):
        add("invalid_name", "error", "name", f"'{name}' does not look like a product name")
    elif len(name) > 300:
        add("name_too_long", "warning", "name", "Product name is unusually long (over 300 characters)")

    if p.price_amount is None:
        add("missing_price", "warning", "price", "No price was found on the source page")
    else:
        amount = Decimal(p.price_amount)
        if amount <= 0:
            add("invalid_price", "error", "price", f"Price {amount} must be greater than zero")
        elif amount > Decimal("10000000"):
            add("suspicious_price", "warning", "price", f"Price {amount} is unusually high; please confirm")
        if not p.currency:
            add("missing_currency", "warning", "currency", "Price has no currency")
    if p.currency and not re.fullmatch(r"[A-Z]{3}", p.currency):
        add("invalid_currency", "error", "currency", f"'{p.currency}' is not an ISO 4217 currency code")
    if p.currency and (p.extra or {}).get("currency_inferred"):
        add("currency_inferred", "info", "currency", "Currency was inferred from an ambiguous symbol (e.g. $); please confirm")

    if p.availability and p.availability not in AVAILABILITY_VALUES:
        add("invalid_availability", "error", "availability", f"'{p.availability}' is not a schema.org availability value")
    if not p.availability:
        add("missing_availability", "info", "availability", "Availability is not stated on the source page")

    if p.product_url and not _valid_http(p.product_url):
        add("invalid_url", "error", "product_url", "Product URL is not a valid http(s) URL")
    if not p.source_url and (p.origin or "website") == "website":
        add("missing_source", "warning", "source_url", "Product has no source URL")

    bad_images = [i.get("url") for i in (p.images or []) if not _valid_http(i.get("url"))]
    if bad_images:
        add("invalid_image_url", "warning", "images", f"{len(bad_images)} image URL(s) are invalid", {"urls": bad_images[:5]})
    if not p.images:
        add("missing_image", "info", "images", "No product image found")

    if not p.category:
        add("missing_category", "info", "category", "No category")
    elif p.category_inferred and "category" not in (p.manual_overrides or []):
        add("category_inferred", "info", "category", "Category was inferred, not stated on the page")

    if not (p.sku or p.gtin or p.mpn):
        add("missing_identifier", "info", "sku", "No SKU, GTIN or MPN found")
    if p.sku and (len(p.sku) > 64 or re.search(r"\s{2,}", p.sku)):
        add("invalid_sku", "warning", "sku", "SKU looks malformed")
    if p.gtin and not gtin_is_valid(p.gtin):
        add("invalid_gtin", "error", "gtin", f"GTIN {p.gtin} fails the check-digit test")
    if not p.description:
        add("missing_description", "info", "description", "No description")

    seen: dict[str, str | None] = {}
    for a in p.attributes:
        if a.value in (None, ""):
            add("empty_attribute", "warning", f"attributes.{a.key}", f"Attribute '{a.label}' has no value")
        if a.key in seen and seen[a.key] != a.value:
            add("conflicting_attribute", "warning", f"attributes.{a.key}", f"Attribute '{a.label}' has conflicting values")
        seen[a.key] = a.value
        if a.value and not a.unit and MEASUREMENT_KEYS.search(a.key) and re.fullmatch(r"[\d.,]+", a.value):
            add("attribute_missing_unit", "info", f"attributes.{a.key}", f"'{a.label}' is numeric but has no unit")

    # merchant attribute definitions: required attributes and value types
    if definitions:
        from app.services.commerce.catalog_rules import check_value

        by_key = {a.key: a for a in p.attributes}
        for d in definitions:
            a = by_key.get(d.key)
            if a is None or a.value in (None, ""):
                if d.is_required:
                    add("missing_required_attribute", "warning", f"attributes.{d.key}",
                        f"Required attribute '{d.name}' is missing")
                continue
            problem = check_value(d, a.value)
            if problem:
                add("attribute_type_mismatch", "warning", f"attributes.{d.key}", f"{d.name}: {problem}",
                    {"expected": d.data_type, "allowed_values": d.allowed_values or []})

    for fl in (p.extra or {}).get("flags", []):
        code = fl.get("code")
        if code not in FLAG_RULES:
            continue
        f = fl.get("field")
        if f and f in (p.manual_overrides or []):
            continue  # the merchant has set this field themselves
        severity, _ = FLAG_RULES[code]
        add(code, severity, f, fl.get("message") or FLAG_RULES[code][1], {k: v for k, v in fl.items() if k not in ("code", "message")})
    # collapse duplicates of same (code, field)
    uniq: dict[tuple, IssueSpec] = {}
    for i in out:
        uniq.setdefault(i.key, i)
    return list(uniq.values())


def quality_status(open_issues: list[IssueSpec] | list[ValidationIssue]) -> str:
    codes = {i.code for i in open_issues}
    sev = {i.severity for i in open_issues}
    if "error" in sev:
        return "invalid"
    if "possible_duplicate" in codes:
        return "duplicate_candidate"
    if codes & {"missing_name", "missing_price"}:
        return "incomplete"
    if "warning" in sev:
        return "needs_review"
    return "valid"


async def sync_issues(session: AsyncSession, product: Product, specs: list[IssueSpec], scan_id: uuid.UUID | None = None,
                      existing: list[ValidationIssue] | None = None) -> None:
    if existing is None:
        existing = list((
            await session.execute(select(ValidationIssue).where(ValidationIssue.product_id == product.id))
        ).scalars().all())
    by_key: dict[tuple, ValidationIssue] = {}
    for e in existing:
        by_key.setdefault((e.code, e.field), e)
    now = datetime.now(timezone.utc)
    wanted = {s.key: s for s in specs}
    for key, issue in by_key.items():
        if key not in wanted and issue.status == "open":
            issue.status = "resolved"
            issue.resolved_at = now
            issue.resolution_note = "Resolved automatically: no longer detected"
            issue.details = {**(issue.details or {}), "auto_resolved": True}
    for key, spec in wanted.items():
        issue = by_key.get(key)
        if issue is None:
            session.add(ValidationIssue(
                business_id=product.business_id, product_id=product.id, scan_id=scan_id, code=spec.code,
                severity=spec.severity, field=spec.field, message=spec.message, details=spec.details,
            ))
            continue
        issue.message, issue.severity = spec.message, spec.severity
        if issue.status == "open":
            issue.details = spec.details
        elif (issue.details or {}).get("auto_resolved"):
            # it came back: reopen
            issue.status, issue.resolved_at, issue.resolution_note = "open", None, None
            issue.details = spec.details
    # status: open issues after sync (merchant-dismissed ones excluded)
    open_specs = [s for k, s in wanted.items() if k not in by_key or by_key[k].status == "open"]
    product.quality_status = quality_status(open_specs)
    product.quality_score = completeness(product)


async def revalidate_business(session: AsyncSession, business_id: uuid.UUID, scan_id: uuid.UUID | None = None) -> None:
    """Recompute issues and quality for every active product of a merchant (duplicates need the whole catalog).

    Issues are loaded in one query rather than one per product.
    """
    from app.models import Category
    from app.services.commerce.catalog_rules import ancestor_ids, applicable, definitions_for

    products = (
        await session.execute(select(Product).where(Product.business_id == business_id, Product.is_removed.is_(False),
                                                    Product.deleted_at.is_(None)))
    ).scalars().all()
    definitions = await definitions_for(session, business_id)
    cat_cache = {c.id: c for c in (await session.execute(select(Category).where(Category.business_id == business_id))).scalars()} if definitions else {}
    dup_specs: dict[uuid.UUID, list[IssueSpec]] = {}
    names = {p.id: p.name for p in products}
    for pair in find_duplicates(products):
        for me, other in ((pair.a, pair.b), (pair.b, pair.a)):
            dup_specs.setdefault(me, []).append(IssueSpec(
                "possible_duplicate", "warning", f"duplicate:{other}",
                f"Possible duplicate of '{names.get(other) or other}' ({pair.reason})",
                {"other_product_id": str(other), "reason": pair.reason, "score": pair.score},
            ))
    issues_by_product: dict[uuid.UUID, list[ValidationIssue]] = {}
    for issue in (await session.execute(
        select(ValidationIssue).where(ValidationIssue.business_id == business_id)
    )).scalars():
        issues_by_product.setdefault(issue.product_id, []).append(issue)
    for p in products:
        defs = applicable(definitions, await ancestor_ids(session, business_id, p.category_id, cat_cache)) if definitions else None
        await sync_issues(session, p, validate_product(p, defs) + dup_specs.get(p.id, []), scan_id,
                          existing=issues_by_product.get(p.id, []))
    await session.flush()
