"""Pricing management: every change is validated and written to immutable price history."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, PriceHistory, Product, ProductEvidence, ProductVariant, ScheduledPriceChange
from app.services.commerce.audit import Actor, record
from app.services.commerce.money import PricingError, aware, fmt, now_utc, price_state, to_money, validate_prices

log = logging.getLogger("agentready.pricing")

_UNSET = object()


def product_pricing(p: Product, include_cost: bool = True, at: datetime | None = None) -> dict:
    effective, status = price_state(p.price_amount, p.sale_price_amount, p.sale_starts_at, p.sale_ends_at, at)
    out = {
        "currency": p.currency,
        "regular_price": fmt(p.price_amount),
        "sale_price": fmt(p.sale_price_amount),
        "sale_starts_at": aware(p.sale_starts_at).isoformat() if p.sale_starts_at else None,
        "sale_ends_at": aware(p.sale_ends_at).isoformat() if p.sale_ends_at else None,
        "effective_price": fmt(effective),
        "price_status": status,
        "tax_class": p.tax_class,
        "tax_included": p.tax_included,
    }
    if include_cost:
        out["cost_price"] = fmt(p.cost_price_amount)
    return out


def variant_effective_price(v: ProductVariant, p: Product, at: datetime | None = None) -> tuple[Decimal | None, str]:
    regular = v.price_amount if v.price_amount is not None else p.price_amount
    sale = v.sale_price_amount if v.price_amount is not None else (v.sale_price_amount or p.sale_price_amount)
    return price_state(regular, sale, p.sale_starts_at, p.sale_ends_at, at)


def _history(db: AsyncSession, p: Product, variant: ProductVariant | None, price_type: str, old, new, currency,
             change_type: str, actor: Actor, reason: str | None, bulk_operation_id=None, scheduled_change_id=None):
    db.add(PriceHistory(
        business_id=p.business_id, product_id=p.id, variant_id=variant.id if variant else None, price_type=price_type,
        old_amount=old, new_amount=new, currency=currency, change_type=change_type, reason=reason,
        actor_type=actor.type, actor_id=actor.id, actor_label=actor.label, bulk_operation_id=bulk_operation_id,
        scheduled_change_id=scheduled_change_id,
    ))


async def set_pricing(
    db: AsyncSession, p: Product, *, actor: Actor, change_type: str = "manual", reason: str | None = None,
    variant: ProductVariant | None = None, regular=_UNSET, sale=_UNSET, cost=_UNSET, currency=_UNSET,
    sale_starts_at=_UNSET, sale_ends_at=_UNSET, tax_class=_UNSET, tax_included=_UNSET,
    bulk_operation_id: uuid.UUID | None = None, scheduled_change_id: uuid.UUID | None = None,
    default_currency: str | None = None, mark_override: bool = True, audit: bool = True,
) -> dict:
    """Validate and apply pricing changes to a product (or one of its variants). Returns {field: (old, new)}."""
    target = variant or p
    cur_regular = target.price_amount
    cur_sale = target.sale_price_amount
    cur_cost = p.cost_price_amount
    cur_currency = (variant.currency if variant and variant.currency else None) or p.currency

    new_regular = cur_regular if regular is _UNSET else to_money(regular)
    new_sale = cur_sale if sale is _UNSET else to_money(sale)
    new_cost = cur_cost if (cost is _UNSET or variant is not None) else to_money(cost)
    new_currency = cur_currency if currency is _UNSET else (currency.upper() if currency else None)
    if new_currency is None and (new_regular is not None or new_sale is not None):
        new_currency = default_currency
    starts = p.sale_starts_at if sale_starts_at is _UNSET else sale_starts_at
    ends = p.sale_ends_at if sale_ends_at is _UNSET else sale_ends_at

    if variant is not None:
        # a variant may inherit the product price; its sale must undercut its own effective regular price
        effective_regular = new_regular if new_regular is not None else p.price_amount
        validate_prices(effective_regular, new_sale, None, new_currency, starts, ends)
    else:
        validate_prices(new_regular, new_sale, new_cost, new_currency, starts, ends)

    changes: dict[str, tuple] = {}
    if new_currency != cur_currency and currency is not _UNSET or (cur_currency is None and new_currency):
        changes["currency"] = (cur_currency, new_currency)
    for price_type, old, new in (("regular", cur_regular, new_regular), ("sale", cur_sale, new_sale),
                                 ("cost", cur_cost, new_cost)):
        if old != new:
            changes[price_type] = (old, new)
            _history(db, p, variant, price_type, old, new, new_currency, change_type, actor, reason,
                     bulk_operation_id, scheduled_change_id)
    if variant is not None:
        variant.price_amount, variant.sale_price_amount = new_regular, new_sale
        if "currency" in changes:
            variant.currency = new_currency
    else:
        p.price_amount, p.sale_price_amount, p.cost_price_amount = new_regular, new_sale, new_cost
        p.currency = new_currency
    for attr, val in (("sale_starts_at", sale_starts_at), ("sale_ends_at", sale_ends_at), ("tax_class", tax_class),
                      ("tax_included", tax_included)):
        if val is not _UNSET and getattr(p, attr) != val:
            changes[attr] = (getattr(p, attr), val)
            setattr(p, attr, val)
    if not changes:
        return {}
    if mark_override and ("regular" in changes or "currency" in changes) and variant is None:
        overrides = set(p.manual_overrides or [])
        overrides.update({"price"} | ({"currency"} if "currency" in changes else set()))
        p.manual_overrides = sorted(overrides)
        if "regular" in changes and actor.type == "user":
            db.add(ProductEvidence(product_id=p.id, business_id=p.business_id, field="price", value=fmt(new_regular),
                                   source_value="Set by merchant in Pricing", source_type="merchant_edit",
                                   extraction_method="merchant"))
    if variant is not None and mark_override:
        overrides = set(p.manual_overrides or [])
        overrides.add("variants")
        p.manual_overrides = sorted(overrides)
    p.version += 1
    p.updated_at = now_utc()
    if audit:
        label = f"variant '{variant.name or variant.sku}' of " if variant else ""
        parts = [f"{k} {fmt(o) if isinstance(o, Decimal) or o is None else o} → {fmt(n) if isinstance(n, Decimal) or n is None else n}"
                 for k, (o, n) in changes.items() if k in ("regular", "sale", "cost", "currency")]
        record(db, p.business_id, actor, "price.updated", "product", p.id,
               f"Price changed on {label}'{p.name}': " + ", ".join(parts or ["sale window/tax updated"]),
               {k: {"from": o, "to": n} for k, (o, n) in changes.items()}, operation_id=bulk_operation_id)
    return changes


# ---------------------------------------------------------------- scheduled changes
async def schedule_change(db: AsyncSession, p: Product, *, actor: Actor, effective_at: datetime, price_type: str,
                          new_amount, variant: ProductVariant | None = None, reason: str | None = None,
                          bulk_operation_id: uuid.UUID | None = None) -> ScheduledPriceChange:
    effective_at = aware(effective_at)
    if effective_at <= now_utc():
        raise PricingError("Scheduled changes must be in the future; edit the price directly to change it now")
    if price_type not in ("regular", "sale"):
        raise PricingError("Only regular and sale prices can be scheduled")
    amount = to_money(new_amount)
    currency = (variant.currency if variant and variant.currency else None) or p.currency
    if price_type == "regular":
        if amount is None:
            raise PricingError("A scheduled regular price needs an amount")
        validate_prices(amount, None, None, currency)
    elif amount is not None:
        regular = (variant.price_amount if variant and variant.price_amount is not None else p.price_amount)
        validate_prices(regular, amount, None, currency)
    change = ScheduledPriceChange(
        business_id=p.business_id, product_id=p.id, variant_id=variant.id if variant else None, price_type=price_type,
        new_amount=amount, currency=currency, effective_at=effective_at, status="scheduled", reason=reason,
        created_by=actor.id, bulk_operation_id=bulk_operation_id,
    )
    db.add(change)
    await db.flush()
    record(db, p.business_id, actor, "price.scheduled", "product", p.id,
           f"Scheduled {price_type} price {fmt(amount) if amount is not None else '(end sale)'} for '{p.name}' at {effective_at.isoformat()}",
           {"scheduled_change_id": str(change.id), "price_type": price_type, "new_amount": amount,
            "effective_at": effective_at}, operation_id=bulk_operation_id)
    return change


def scheduled_dict(c: ScheduledPriceChange, product_name: str | None = None) -> dict:
    return {
        "id": str(c.id), "product_id": str(c.product_id), "product_name": product_name,
        "variant_id": str(c.variant_id) if c.variant_id else None, "price_type": c.price_type,
        "new_amount": fmt(c.new_amount), "currency": c.currency, "effective_at": aware(c.effective_at).isoformat(),
        "status": c.status, "reason": c.reason, "error": c.error,
        "applied_at": aware(c.applied_at).isoformat() if c.applied_at else None,
        "cancelled_at": aware(c.cancelled_at).isoformat() if c.cancelled_at else None,
        "created_at": aware(c.created_at).isoformat() if c.created_at else None,
        "bulk_operation_id": str(c.bulk_operation_id) if c.bulk_operation_id else None,
    }


async def apply_due_price_changes(db: AsyncSession, at: datetime | None = None, limit: int = 200,
                                  business_ids: list | None = None) -> dict:
    """Apply scheduled price changes whose time has come. Safe to run concurrently: each change is claimed
    with a conditional UPDATE (scheduled -> applying) before it is applied, so no change is applied twice.

    ``business_ids`` limits the run to some merchants (used by the public agent API so a price is never served
    stale between scheduler ticks)."""
    at = at or now_utc()
    stmt = select(ScheduledPriceChange.id).where(ScheduledPriceChange.status == "scheduled",
                                                 ScheduledPriceChange.effective_at <= at)
    if business_ids is not None:
        stmt = stmt.where(ScheduledPriceChange.business_id.in_(business_ids))
    due = (await db.execute(stmt.order_by(ScheduledPriceChange.effective_at).limit(limit))).scalars().all()
    applied = failed = 0
    for cid in due:
        claim = await db.execute(update(ScheduledPriceChange).where(
            ScheduledPriceChange.id == cid, ScheduledPriceChange.status == "scheduled").values(status="applying"))
        await db.commit()
        if claim.rowcount != 1:
            continue  # another worker took it
        change = await db.get(ScheduledPriceChange, cid, populate_existing=True)
        p = await db.get(Product, change.product_id)
        variant = await db.get(ProductVariant, change.variant_id) if change.variant_id else None
        try:
            if p is None or p.deleted_at is not None:
                raise PricingError("The product no longer exists")
            business = await db.get(Business, p.business_id)
            current_currency = (variant.currency if variant and variant.currency else None) or p.currency
            if change.currency and current_currency and change.currency != current_currency:
                raise PricingError(f"Currency changed from {change.currency} to {current_currency} since scheduling")
            kwargs = {"regular": change.new_amount} if change.price_type == "regular" else {"sale": change.new_amount}
            if change.price_type == "sale" and change.new_amount is None:
                kwargs.update(sale_starts_at=None, sale_ends_at=None) if variant is None else None
            await set_pricing(db, p, actor=Actor.system("Scheduled price change"), change_type="scheduled",
                              reason=change.reason, variant=variant, scheduled_change_id=change.id,
                              bulk_operation_id=change.bulk_operation_id,
                              default_currency=business.default_currency if business else None, **kwargs)
            change.status, change.applied_at = "applied", now_utc()
            applied += 1
        except PricingError as e:
            await db.rollback()
            change = await db.get(ScheduledPriceChange, cid, populate_existing=True)
            change.status, change.error = "failed", str(e)
            if p is not None:
                record(db, change.business_id, Actor.system("Scheduled price change"), "price.schedule_failed",
                       "product", change.product_id, f"Scheduled price change could not be applied: {e}")
            failed += 1
        await db.commit()
    return {"applied": applied, "failed": failed}
