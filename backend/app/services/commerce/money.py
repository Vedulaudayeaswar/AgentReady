"""Money helpers. All arithmetic uses Decimal; amounts are quantized to 2 places (ROUND_HALF_UP)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("1000000000")


class PricingError(ValueError):
    """A price change that violates a pricing rule (message is safe to show to merchants)."""


def to_money(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, float):
        v = repr(v)  # never go through binary floating point
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise PricingError(f"'{v}' is not a valid amount")
    if not d.is_finite():
        raise PricingError("Amount must be a finite number")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(d: Decimal | None) -> str | None:
    return None if d is None else format(Decimal(d).quantize(CENT), "f")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def sale_window_active(starts: datetime | None, ends: datetime | None, at: datetime | None = None) -> bool:
    at = at or now_utc()
    starts, ends = aware(starts), aware(ends)
    return (starts is None or starts <= at) and (ends is None or at < ends)


def price_state(regular: Decimal | None, sale: Decimal | None, starts: datetime | None, ends: datetime | None,
                at: datetime | None = None) -> tuple[Decimal | None, str]:
    """(effective price, status) — status: no_price | regular | on_sale | sale_scheduled."""
    at = at or now_utc()
    if regular is None and sale is None:
        return None, "no_price"
    if sale is not None and sale_window_active(starts, ends, at):
        return sale, "on_sale"
    if sale is not None and aware(starts) is not None and aware(starts) > at:
        return regular, "sale_scheduled"
    return regular, "regular" if regular is not None else "no_price"


def adjust(amount: Decimal, mode: str, value: Decimal) -> Decimal:
    """Apply a price adjustment. Percentages are of the current amount."""
    if mode == "percent_increase":
        new = amount * (Decimal(1) + value / Decimal(100))
    elif mode == "percent_decrease":
        new = amount * (Decimal(1) - value / Decimal(100))
    elif mode == "fixed_increase":
        new = amount + value
    elif mode == "fixed_decrease":
        new = amount - value
    elif mode == "set":
        new = value
    else:
        raise PricingError(f"Unknown adjustment '{mode}'")
    return new.quantize(CENT, rounding=ROUND_HALF_UP)


def validate_prices(regular: Decimal | None, sale: Decimal | None, cost: Decimal | None, currency: str | None,
                    starts: datetime | None = None, ends: datetime | None = None) -> None:
    for label, v in (("Regular price", regular), ("Sale price", sale)):
        if v is not None:
            if v <= 0:
                raise PricingError(f"{label} must be greater than zero")
            if v > MAX_AMOUNT:
                raise PricingError(f"{label} is too large")
    if cost is not None and (cost < 0 or cost > MAX_AMOUNT):
        raise PricingError("Cost price must be between 0 and 1,000,000,000")
    if sale is not None:
        if regular is None:
            raise PricingError("Set a regular price before adding a sale price")
        if sale >= regular:
            raise PricingError(f"Sale price {fmt(sale)} must be lower than the regular price {fmt(regular)}")
    if (regular is not None or sale is not None) and not currency:
        raise PricingError("A currency is required when a price is set (set one on the product or a default in Settings)")
    if starts is not None and ends is not None and aware(ends) <= aware(starts):
        raise PricingError("Sale end must be after sale start")
