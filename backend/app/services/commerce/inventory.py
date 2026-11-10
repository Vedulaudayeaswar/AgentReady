"""Inventory management.

Model: available = on_hand - reserved. Every quantity change writes an immutable
InventoryTransaction. Updates use optimistic concurrency (``inventory_version``): the
row is updated only if its version is unchanged since it was read, otherwise the
operation re-reads and retries. This is safe on PostgreSQL (row locks + re-check) and
SQLite (single writer) without lost updates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, InventoryTransaction, Product, ProductVariant
from app.services.commerce.audit import Actor, record
from app.services.commerce.money import now_utc

MAX_QUANTITY = 1_000_000_000
OPERATIONS = ("set", "increase", "decrease", "mark_out_of_stock", "reserve", "release")


class InventoryError(ValueError):
    pass


class InventoryConflict(InventoryError):
    pass


def stock_status(track: bool, on_hand: int, reserved: int, threshold: int | None, backorder: str) -> str:
    if not track:
        return "untracked"
    available = on_hand - reserved
    if available <= 0:
        return "backorder" if backorder == "allow" else "out_of_stock"
    if threshold is not None and available <= threshold:
        return "low_stock"
    return "in_stock"


AVAILABILITY_FOR = {"in_stock": "InStock", "low_stock": "InStock", "out_of_stock": "OutOfStock", "backorder": "BackOrder"}


def effective_threshold(row, business: Business | None) -> int | None:
    if row.low_stock_threshold is not None:
        return row.low_stock_threshold
    return business.default_low_stock_threshold if business is not None else None


def aggregate_status(own: str, variant_statuses: list[str]) -> str:
    tracked = [s for s in variant_statuses if s != "untracked"]
    if not tracked:
        return own
    if all(s in ("out_of_stock", "backorder") for s in tracked):
        return "backorder" if "backorder" in tracked else "out_of_stock"
    if any(s in ("low_stock", "out_of_stock", "backorder") for s in tracked):
        return "low_stock"
    return "in_stock"


def inventory_dict(row, business: Business | None, include_private: bool = True) -> dict:
    available = row.stock_on_hand - row.stock_reserved
    out = {"track_inventory": row.track_inventory, "stock_status": row.stock_status}
    if include_private:
        out.update({
            "on_hand": row.stock_on_hand, "reserved": row.stock_reserved, "available": available,
            "low_stock_threshold": row.low_stock_threshold, "effective_low_stock_threshold": effective_threshold(row, business),
            "backorder_policy": row.backorder_policy, "location": row.inventory_location, "version": row.inventory_version,
        })
    return out


async def refresh_product_status(db: AsyncSession, product: Product, business: Business | None) -> None:
    """Recompute a product's stored stock status (and availability when inventory is tracked)."""
    own = stock_status(product.track_inventory, product.stock_on_hand, product.stock_reserved,
                       effective_threshold(product, business), product.backorder_policy)
    variants = (await db.execute(select(ProductVariant).where(
        ProductVariant.product_id == product.id, ProductVariant.archived_at.is_(None),
        ProductVariant.is_enabled.is_(True)).execution_options(populate_existing=True))).scalars().all()
    status = aggregate_status(own, [v.stock_status for v in variants])
    product.stock_status = status
    if status != "untracked":
        product.availability = AVAILABILITY_FOR[status]
        overrides = set(product.manual_overrides or [])
        if "availability" not in overrides:
            product.manual_overrides = sorted(overrides | {"availability"})


@dataclass
class StockChange:
    previous: int
    adjustment: int
    new: int
    field: str
    status: str
    transaction_id: uuid.UUID


async def change_stock(
    db: AsyncSession, product: Product, *, actor: Actor, operation: str, quantity: int = 0,
    variant: ProductVariant | None = None, reason: str | None = None, bulk_operation_id: uuid.UUID | None = None,
    related_operation: str | None = None, transaction_type: str | None = None, max_attempts: int = 8,
) -> StockChange:
    if operation not in OPERATIONS:
        raise InventoryError(f"Unknown inventory operation '{operation}'")
    if quantity < 0 or quantity > MAX_QUANTITY:
        raise InventoryError("Quantity must be between 0 and 1,000,000,000")
    if operation in ("increase", "decrease", "reserve", "release") and quantity == 0:
        raise InventoryError("Quantity must be greater than zero")
    model = ProductVariant if variant is not None else Product
    row_id = variant.id if variant is not None else product.id
    business = await db.get(Business, product.business_id)

    for _ in range(max_attempts):
        row = (await db.execute(select(model).where(model.id == row_id).execution_options(populate_existing=True))).scalar_one()
        if not row.track_inventory:
            raise InventoryError("Inventory tracking is disabled for this item. Enable tracking first.")
        on_hand, reserved, version = row.stock_on_hand, row.stock_reserved, row.inventory_version
        field = "reserved" if operation in ("reserve", "release") else "on_hand"
        prev = reserved if field == "reserved" else on_hand
        new = {
            "set": quantity, "increase": on_hand + quantity, "decrease": on_hand - quantity, "mark_out_of_stock": reserved,
            "reserve": reserved + quantity, "release": reserved - quantity,
        }[operation]
        new_on_hand = new if field == "on_hand" else on_hand
        new_reserved = new if field == "reserved" else reserved
        if new_reserved < 0:
            raise InventoryError(f"Cannot release {quantity}: only {reserved} reserved")
        allow_negative = row.backorder_policy == "allow"
        if new_on_hand < 0 and not allow_negative:
            raise InventoryError(f"Not enough stock: {on_hand} on hand, cannot remove {quantity}. "
                                 "Enable backorders to allow negative stock.")
        if new_on_hand - new_reserved < 0 and not allow_negative:
            raise InventoryError(f"Cannot reserve more than is available ({on_hand - reserved} available)")
        if abs(new_on_hand) > MAX_QUANTITY:
            raise InventoryError("Quantity is too large")
        status = stock_status(True, new_on_hand, new_reserved, effective_threshold(row, business), row.backorder_policy)
        values = {"stock_on_hand": new_on_hand, "stock_reserved": new_reserved, "inventory_version": version + 1,
                  "stock_status": status, "availability": AVAILABILITY_FOR[status]}
        res = await db.execute(update(model).where(model.id == row_id, model.inventory_version == version).values(**values)
                               .execution_options(synchronize_session=False))
        if res.rowcount == 1:
            break
    else:
        raise InventoryConflict("The stock level was changed by another request at the same time. Please retry.")

    tx = InventoryTransaction(
        business_id=product.business_id, product_id=product.id, variant_id=variant.id if variant else None,
        transaction_type=transaction_type or operation, quantity_field=field, previous_quantity=prev,
        adjustment=new - prev, new_quantity=new, reason=reason, actor_type=actor.type, actor_id=actor.id,
        actor_label=actor.label, bulk_operation_id=bulk_operation_id, related_operation=related_operation,
    )
    db.add(tx)
    await db.flush()
    target = await db.get(model, row_id, populate_existing=True)
    if variant is not None:
        await refresh_product_status(db, product, business)
    else:
        await refresh_product_status(db, target, business)
    product.updated_at = now_utc()
    # the stock row was changed with a core UPDATE (optimistic concurrency), which the ORM change hook cannot see
    from app.services.distribution.events import note_event

    note_event(db, product.business_id, product.id, "product.availability_changed",
               variant.id if variant else None, ["stock"])
    if variant is not None:
        note_event(db, product.business_id, product.id, "product.variant_updated", variant.id, ["stock"])
    label = f"variant '{variant.name or variant.sku}' of " if variant else ""
    record(db, product.business_id, actor, "inventory.adjusted", "product", product.id,
           f"Stock {field.replace('_', ' ')} for {label}'{product.name}': {prev} → {new} ({operation.replace('_', ' ')})"
           + (f" — {reason}" if reason else ""),
           {"field": field, "previous": prev, "new": new, "operation": operation, "variant_id": variant.id if variant else None},
           operation_id=bulk_operation_id)
    return StockChange(prev, new - prev, new, field, status, tx.id)


async def update_settings(db: AsyncSession, product: Product, *, actor: Actor, variant: ProductVariant | None = None,
                          **settings) -> dict:
    """Tracking, threshold, backorder policy and location. Quantities change only through change_stock()."""
    row = variant or product
    business = await db.get(Business, product.business_id)
    changed = {}
    for k in ("track_inventory", "low_stock_threshold", "backorder_policy", "inventory_location"):
        if k in settings and getattr(row, k) != settings[k]:
            if k == "backorder_policy" and settings[k] not in ("deny", "allow"):
                raise InventoryError("Backorder policy must be 'deny' or 'allow'")
            if k == "low_stock_threshold" and settings[k] is not None and not (0 <= settings[k] <= MAX_QUANTITY):
                raise InventoryError("Low-stock threshold must be zero or more")
            changed[k] = {"from": getattr(row, k), "to": settings[k]}
            setattr(row, k, settings[k])
    if row.backorder_policy == "deny" and row.track_inventory and row.stock_on_hand - row.stock_reserved < 0 and "backorder_policy" in changed:
        raise InventoryError("Stock is negative; adjust it to zero or more before disabling backorders")
    if not changed:
        return {}
    row.stock_status = stock_status(row.track_inventory, row.stock_on_hand, row.stock_reserved,
                                    effective_threshold(row, business), row.backorder_policy)
    if variant is not None and row.stock_status != "untracked":
        variant.availability = AVAILABILITY_FOR[row.stock_status]
    row.inventory_version += 1
    await db.flush()
    await refresh_product_status(db, product, business)
    product.updated_at = now_utc()
    # the stock row was changed with a core UPDATE (optimistic concurrency), which the ORM change hook cannot see
    from app.services.distribution.events import note_event

    note_event(db, product.business_id, product.id, "product.availability_changed",
               variant.id if variant else None, ["stock"])
    if variant is not None:
        note_event(db, product.business_id, product.id, "product.variant_updated", variant.id, ["stock"])
    label = f"variant '{variant.name or variant.sku}' of " if variant else ""
    record(db, product.business_id, actor, "inventory.settings_updated", "product", product.id,
           f"Inventory settings changed for {label}'{product.name}': " + ", ".join(changed), changed)
    return changed
