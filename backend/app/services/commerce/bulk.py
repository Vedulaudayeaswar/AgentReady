"""Bulk operations: preview -> confirm -> execute -> per-item results.

``preview`` validates every selected product and computes the proposed values without writing.
``execute`` re-validates each item at execution time (data may have changed since the preview),
applies it inside its own savepoint, and records success/failure per item. Operations over
BACKGROUND_THRESHOLD items run as a background task with real progress counters.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BulkOperation,
    BulkOperationItem,
    Business,
    Category,
    Product,
    ProductAttribute,
    ValidationIssue,
)
from app.services.commerce import lifecycle
from app.services.commerce.audit import Actor, record
from app.services.commerce.catalog_rules import CatalogRuleError, assign_category, get_category
from app.services.commerce.inventory import InventoryError, change_stock, update_settings
from app.services.commerce.money import PricingError, adjust, aware, fmt, now_utc, to_money, validate_prices
from app.services.commerce.pricing import schedule_change, set_pricing
from app.services.normalization.normalize import attribute_key, split_value_unit

log = logging.getLogger("agentready.bulk")

BACKGROUND_THRESHOLD = 100
MAX_ITEMS = 5000
PRICE_MODES = ("percent_increase", "percent_decrease", "fixed_increase", "fixed_decrease", "set")
OPERATIONS = {
    "approve": "Approve", "send_to_review": "Send back to review", "reject": "Reject",
    "publish": "Publish", "unpublish": "Unpublish",
    "archive": "Archive", "restore": "Restore", "activate": "Set active", "discontinue": "Mark discontinued",
    "delete": "Delete",
    "assign_category": "Assign category", "set_brand": "Update brand", "set_product_type": "Set product type",
    "add_attribute": "Add attribute", "remove_attribute": "Remove attribute",
    "price_adjust": "Adjust prices", "set_sale": "Set sale price", "clear_sale": "End sale",
    "schedule_price": "Schedule price change",
    "inventory_adjust": "Adjust inventory", "inventory_settings": "Inventory settings",
}


class ItemError(ValueError):
    pass


def _err(e: Exception) -> str:
    return str(e) or type(e).__name__


class BulkContext:
    """Per-operation state shared by all items (loaded once)."""

    def __init__(self, business: Business, params: dict, blocking: dict[uuid.UUID, int], category: Category | None):
        self.business = business
        self.params = params
        self.blocking = blocking
        self.category = category


def validate_params(operation: str, params: dict) -> dict:
    """Normalise and validate operation parameters (raises ValueError with a merchant-readable message)."""
    p = dict(params or {})
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown operation '{operation}'")
    if operation in ("price_adjust", "schedule_price"):
        if p.get("mode") not in PRICE_MODES:
            raise ValueError(f"mode must be one of {', '.join(PRICE_MODES)}")
        p["target"] = p.get("target") or "regular"
        if p["target"] not in ("regular", "sale"):
            raise ValueError("target must be 'regular' or 'sale'")
        v = to_money(p.get("value"))
        if v is None or v < 0:
            raise ValueError("value must be a positive amount or percentage")
        if p["mode"].startswith("percent") and v > 1000:
            raise ValueError("Percentages above 1000% are not allowed")
        if p["mode"] == "percent_decrease" and v >= 100:
            raise ValueError("A decrease of 100% or more would make prices zero or negative")
        p["value"] = fmt(v)
        if not p["mode"].startswith("percent"):
            cur = (p.get("currency") or "").upper()
            if len(cur) != 3:
                raise ValueError("Fixed-amount changes need a currency (prices in other currencies are not changed)")
            p["currency"] = cur
    if operation == "schedule_price":
        if not p.get("effective_at"):
            raise ValueError("effective_at is required")
        dt = p["effective_at"] if isinstance(p["effective_at"], datetime) else datetime.fromisoformat(str(p["effective_at"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("effective_at must include a timezone offset")
        if dt <= now_utc():
            raise ValueError("effective_at must be in the future")
        p["effective_at"] = dt.isoformat()
    if operation == "set_sale":
        if p.get("mode") not in ("percent_off", "fixed_price"):
            raise ValueError("mode must be 'percent_off' or 'fixed_price'")
        v = to_money(p.get("value"))
        if v is None or v <= 0:
            raise ValueError("value must be greater than zero")
        if p["mode"] == "percent_off" and v >= 100:
            raise ValueError("percent_off must be below 100")
        p["value"] = fmt(v)
        if p["mode"] == "fixed_price":
            cur = (p.get("currency") or "").upper()
            if len(cur) != 3:
                raise ValueError("A fixed sale price needs a currency")
            p["currency"] = cur
        for k in ("starts_at", "ends_at"):
            if p.get(k):
                dt = datetime.fromisoformat(str(p[k]).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError(f"{k} must include a timezone offset")
                p[k] = dt.isoformat()
    if operation == "inventory_adjust":
        if p.get("operation") not in ("set", "increase", "decrease", "mark_out_of_stock"):
            raise ValueError("operation must be set, increase, decrease or mark_out_of_stock")
        q = p.get("quantity", 0)
        if not isinstance(q, int) or isinstance(q, bool) or q < 0 or q > 1_000_000_000:
            raise ValueError("quantity must be a whole number of zero or more")
        if p["operation"] in ("increase", "decrease") and q == 0:
            raise ValueError("quantity must be greater than zero")
    if operation == "inventory_settings":
        allowed = {"track_inventory", "low_stock_threshold", "backorder_policy"}
        if not set(p) & allowed or set(p) - allowed:
            raise ValueError("Provide track_inventory, low_stock_threshold and/or backorder_policy")
        if "backorder_policy" in p and p["backorder_policy"] not in ("deny", "allow"):
            raise ValueError("backorder_policy must be deny or allow")
        if p.get("low_stock_threshold") is not None and (not isinstance(p["low_stock_threshold"], int) or p["low_stock_threshold"] < 0):
            raise ValueError("low_stock_threshold must be zero or more")
    if operation == "set_brand":
        b = (p.get("brand") or "").strip()
        if len(b) > 200:
            raise ValueError("brand is too long")
        p["brand"] = b or None
    if operation == "set_product_type":
        t = (p.get("product_type") or "").strip()
        if len(t) > 60:
            raise ValueError("product_type is too long")
        p["product_type"] = t or None
    if operation == "add_attribute":
        label = (p.get("label") or "").strip()
        if not label or len(label) > 200:
            raise ValueError("label is required (max 200 characters)")
        p["label"] = label
        p["key"] = (p.get("key") or attribute_key(label))[:120]
        p["value"] = (str(p.get("value")) if p.get("value") not in (None, "") else None)
        if p["value"] and len(p["value"]) > 2000:
            raise ValueError("value is too long")
    if operation == "remove_attribute" and not (p.get("key") or "").strip():
        raise ValueError("key is required")
    return p


def _price_snapshot(prod: Product) -> dict:
    return {"currency": prod.currency, "regular_price": fmt(prod.price_amount), "sale_price": fmt(prod.sale_price_amount)}


def plan(op: str, prod: Product, bc: BulkContext) -> tuple[dict, dict]:
    """(before, after) for one product, or raise ItemError. Never writes."""
    params = bc.params
    if prod.deleted_at is not None and op not in ("restore",):
        raise ItemError("Product is deleted")
    if op in ("approve",):
        if bc.blocking.get(prod.id):
            raise ItemError(f"{bc.blocking[prod.id]} blocking validation error(s) — fix them first")
        return {"approval_status": prod.approval_status}, {"approval_status": "approved",
                                                           "publication_status": "published" if prod.lifecycle_status in ("active", "discontinued") else prod.publication_status}
    if op == "send_to_review":
        return {"approval_status": prod.approval_status}, {"approval_status": "needs_review"}
    if op == "reject":
        return {"approval_status": prod.approval_status}, {"approval_status": "rejected"}
    if op == "publish":
        try:
            lifecycle.check_publish(prod)
        except lifecycle.LifecycleError as e:
            raise ItemError(str(e))
        return {"publication_status": prod.publication_status}, {"publication_status": "published"}
    if op == "unpublish":
        return {"publication_status": prod.publication_status}, {"publication_status": "unpublished"}
    if op in ("archive", "activate", "discontinue"):
        target = {"archive": "archived", "activate": "active", "discontinue": "discontinued"}[op]
        return {"lifecycle_status": prod.lifecycle_status}, {"lifecycle_status": target}
    if op == "restore":
        if prod.deleted_at is None and prod.lifecycle_status != "archived":
            raise ItemError("Not archived or deleted")
        return ({"lifecycle_status": prod.lifecycle_status, "deleted": prod.deleted_at is not None},
                {"lifecycle_status": "active" if prod.lifecycle_status == "archived" else prod.lifecycle_status, "deleted": False})
    if op == "delete":
        return {"deleted": False}, {"deleted": True}
    if op == "assign_category":
        return {"category": prod.category}, {"category": bc.category.path if bc.category else None}
    if op == "set_brand":
        return {"brand": prod.brand}, {"brand": params["brand"]}
    if op == "set_product_type":
        return {"product_type": prod.product_type}, {"product_type": params["product_type"]}
    if op == "add_attribute":
        existing = next((a for a in prod.attributes if a.key == params["key"]), None)
        return ({"attribute": {"key": params["key"], "value": existing.value if existing else None}},
                {"attribute": {"key": params["key"], "value": params["value"], "unit": params.get("unit")}})
    if op == "remove_attribute":
        existing = next((a for a in prod.attributes if a.key == params["key"]), None)
        if existing is None:
            raise ItemError(f"No attribute '{params['key']}'")
        return {"attribute": {"key": existing.key, "value": existing.value}}, {"attribute": None}
    if op in ("price_adjust", "schedule_price"):
        target = params["target"]
        current = prod.price_amount if target == "regular" else prod.sale_price_amount
        if current is None:
            raise ItemError("No regular price set" if target == "regular" else "No sale price set")
        if not params["mode"].startswith("percent") and prod.currency != params["currency"]:
            raise ItemError(f"Currency mismatch: product is priced in {prod.currency or 'no currency'}, "
                            f"adjustment is in {params['currency']}")
        new = adjust(current, params["mode"], Decimal(params["value"]))
        regular = new if target == "regular" else prod.price_amount
        sale = new if target == "sale" else prod.sale_price_amount
        if op == "schedule_price":
            sale = None if target == "regular" else sale  # the sale check happens when the change is applied
        try:
            validate_prices(regular, sale, None, prod.currency or bc.business.default_currency)
        except PricingError as e:
            raise ItemError(str(e))
        before = _price_snapshot(prod)
        after = {**before, f"{target}_price": fmt(new)}
        if op == "schedule_price":
            after["effective_at"] = params["effective_at"]
        return before, after
    if op == "set_sale":
        if prod.price_amount is None:
            raise ItemError("No regular price set")
        if params["mode"] == "percent_off":
            sale = adjust(prod.price_amount, "percent_decrease", Decimal(params["value"]))
        else:
            if prod.currency != params["currency"]:
                raise ItemError(f"Currency mismatch: product is priced in {prod.currency or 'no currency'}")
            sale = Decimal(params["value"])
        try:
            validate_prices(prod.price_amount, sale, None, prod.currency,
                            datetime.fromisoformat(params["starts_at"]) if params.get("starts_at") else None,
                            datetime.fromisoformat(params["ends_at"]) if params.get("ends_at") else None)
        except PricingError as e:
            raise ItemError(str(e))
        before = _price_snapshot(prod)
        return before, {**before, "sale_price": fmt(sale), "sale_starts_at": params.get("starts_at"),
                        "sale_ends_at": params.get("ends_at")}
    if op == "clear_sale":
        if prod.sale_price_amount is None:
            raise ItemError("No sale price set")
        before = _price_snapshot(prod)
        return before, {**before, "sale_price": None}
    if op == "inventory_adjust":
        q, o = params.get("quantity", 0), params["operation"]
        if any(v.archived_at is None and v.track_inventory for v in prod.variants):
            raise ItemError("Stock is tracked per variant; adjust the variants individually")
        if not prod.track_inventory:
            if o == "mark_out_of_stock":
                return {"availability": prod.availability}, {"availability": "OutOfStock"}
            raise ItemError("Inventory tracking is disabled for this product")
        on_hand, reserved = prod.stock_on_hand, prod.stock_reserved
        new = {"set": q, "increase": on_hand + q, "decrease": on_hand - q, "mark_out_of_stock": reserved}[o]
        if new < 0 and prod.backorder_policy != "allow":
            raise ItemError(f"Not enough stock ({on_hand} on hand)")
        return {"on_hand": on_hand, "available": on_hand - reserved}, {"on_hand": new, "available": new - reserved}
    if op == "inventory_settings":
        before = {k: getattr(prod, k) for k in params}
        return before, dict(params)
    raise ItemError(f"Unsupported operation {op}")


async def apply_item(db: AsyncSession, op: str, prod: Product, bc: BulkContext, actor: Actor, op_id: uuid.UUID) -> None:
    params = bc.params
    try:
        if op == "approve":
            lifecycle.set_approval(db, prod, "approved", actor, op_id)
        elif op == "send_to_review":
            lifecycle.set_approval(db, prod, "needs_review", actor, op_id)
        elif op == "reject":
            lifecycle.set_approval(db, prod, "rejected", actor, op_id)
        elif op in ("publish", "unpublish"):
            lifecycle.set_publication(db, prod, "published" if op == "publish" else "unpublished", actor, op_id)
        elif op in ("archive", "activate", "discontinue"):
            lifecycle.set_lifecycle(db, prod, {"archive": "archived", "activate": "active", "discontinue": "discontinued"}[op],
                                    actor, op_id)
        elif op == "restore":
            lifecycle.restore(db, prod, actor, op_id)
        elif op == "delete":
            lifecycle.soft_delete(db, prod, actor, op_id)
        elif op == "assign_category":
            await assign_category(db, prod, bc.category, actor, op_id)
        elif op in ("set_brand", "set_product_type"):
            field = "brand" if op == "set_brand" else "product_type"
            old = getattr(prod, field)
            setattr(prod, field, params[field])
            if field == "brand":
                prod.manual_overrides = sorted(set(prod.manual_overrides or []) | {"brand"})
            prod.version += 1
            prod.updated_at = now_utc()
            record(db, prod.business_id, actor, "product.updated", "product", prod.id,
                   f"{field.replace('_', ' ').title()} of '{prod.name}': {old or 'none'} → {params[field] or 'none'}",
                   {field: {"from": old, "to": params[field]}}, op_id)
        elif op == "add_attribute":
            value, unit = (params["value"], params.get("unit")) if params.get("unit") else split_value_unit(params["value"])
            keep = [a for a in prod.attributes if a.key != params["key"]]
            prod.attributes = keep + [ProductAttribute(key=params["key"], label=params["label"], value=value,
                                                       unit=params.get("unit") or unit, raw_value=params["value"],
                                                       source="merchant", position=len(keep))]
            prod.version += 1
            prod.updated_at = now_utc()
            record(db, prod.business_id, actor, "product.attribute_added", "product", prod.id,
                   f"Attribute '{params['label']}' set on '{prod.name}'", {"key": params["key"], "value": params["value"]}, op_id)
        elif op == "remove_attribute":
            prod.attributes = [a for a in prod.attributes if a.key != params["key"]]
            prod.manual_overrides = sorted(set(prod.manual_overrides or []) | {"attributes"})
            prod.version += 1
            prod.updated_at = now_utc()
            record(db, prod.business_id, actor, "product.attribute_removed", "product", prod.id,
                   f"Attribute '{params['key']}' removed from '{prod.name}'", {"key": params["key"]}, op_id)
        elif op == "price_adjust":
            target = params["target"]
            current = prod.price_amount if target == "regular" else prod.sale_price_amount
            new = adjust(current, params["mode"], Decimal(params["value"]))
            await set_pricing(db, prod, actor=actor, change_type="bulk", reason=params.get("reason"),
                              bulk_operation_id=op_id, default_currency=bc.business.default_currency,
                              **({"regular": new} if target == "regular" else {"sale": new}))
            if params.get("include_variants", True) and target == "regular":
                for v in prod.variants:
                    if v.archived_at is None and v.price_amount is not None:
                        vcur = v.currency or prod.currency
                        if not params["mode"].startswith("percent") and vcur != params["currency"]:
                            continue
                        await set_pricing(db, prod, actor=actor, change_type="bulk", variant=v, bulk_operation_id=op_id,
                                          regular=adjust(v.price_amount, params["mode"], Decimal(params["value"])),
                                          reason=params.get("reason"))
        elif op == "schedule_price":
            target = params["target"]
            current = prod.price_amount if target == "regular" else prod.sale_price_amount
            new = adjust(current, params["mode"], Decimal(params["value"]))
            await schedule_change(db, prod, actor=actor, effective_at=datetime.fromisoformat(params["effective_at"]),
                                  price_type=target, new_amount=new, reason=params.get("reason"), bulk_operation_id=op_id)
        elif op == "set_sale":
            sale = (adjust(prod.price_amount, "percent_decrease", Decimal(params["value"]))
                    if params["mode"] == "percent_off" else Decimal(params["value"]))
            await set_pricing(db, prod, actor=actor, change_type="bulk", sale=sale, bulk_operation_id=op_id,
                              reason=params.get("reason"),
                              sale_starts_at=datetime.fromisoformat(params["starts_at"]) if params.get("starts_at") else None,
                              sale_ends_at=datetime.fromisoformat(params["ends_at"]) if params.get("ends_at") else None)
        elif op == "clear_sale":
            await set_pricing(db, prod, actor=actor, change_type="bulk", sale=None, sale_starts_at=None,
                              sale_ends_at=None, bulk_operation_id=op_id, reason=params.get("reason"))
        elif op == "inventory_adjust":
            if not prod.track_inventory and params["operation"] == "mark_out_of_stock":
                old = prod.availability
                prod.availability = "OutOfStock"
                prod.manual_overrides = sorted(set(prod.manual_overrides or []) | {"availability"})
                prod.version += 1
                prod.updated_at = now_utc()
                record(db, prod.business_id, actor, "inventory.marked_out_of_stock", "product", prod.id,
                       f"'{prod.name}' marked out of stock (inventory not tracked)", {"availability": {"from": old, "to": "OutOfStock"}}, op_id)
            else:
                await change_stock(db, prod, actor=actor, operation=params["operation"], quantity=params.get("quantity", 0),
                                   reason=params.get("reason"), bulk_operation_id=op_id, related_operation=f"bulk:{op_id}")
        elif op == "inventory_settings":
            await update_settings(db, prod, actor=actor, **params)
        else:
            raise ItemError(f"Unsupported operation {op}")
    except (PricingError, InventoryError, lifecycle.LifecycleError, CatalogRuleError) as e:
        raise ItemError(_err(e))


async def load_context(db: AsyncSession, business: Business, operation: str, params: dict, ids: list[uuid.UUID]) -> BulkContext:
    blocking: dict[uuid.UUID, int] = {}
    if operation == "approve" and ids:
        for pid in (await db.execute(select(ValidationIssue.product_id).where(
                ValidationIssue.product_id.in_(ids), ValidationIssue.status == "open", ValidationIssue.severity == "error"
        ))).scalars():
            blocking[pid] = blocking.get(pid, 0) + 1
    category = None
    if operation == "assign_category" and params.get("category_id"):
        try:
            category = await get_category(db, business.id, params["category_id"])
        except CatalogRuleError as e:
            raise ValueError(str(e))
        if category.archived_at is not None:
            raise ValueError("That category is archived")
    return BulkContext(business, params, blocking, category)


async def owned_products(db: AsyncSession, business_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, Product]:
    rows = (await db.execute(select(Product).where(Product.business_id == business_id, Product.id.in_(ids)))).scalars().all()
    return {p.id: p for p in rows}


async def preview(db: AsyncSession, business: Business, operation: str, params: dict, ids: list[uuid.UUID]) -> dict:
    params = validate_params(operation, params)
    bc = await load_context(db, business, operation, params, ids)
    products = await owned_products(db, business.id, ids)
    items, valid, invalid = [], 0, 0
    for pid in ids:
        prod = products.get(pid)
        if prod is None:
            items.append({"product_id": str(pid), "name": None, "status": "invalid", "error": "Product not found"})
            invalid += 1
            continue
        try:
            before, after = plan(operation, prod, bc)
            items.append({"product_id": str(pid), "name": prod.name, "sku": prod.sku, "status": "ok", "before": before,
                          "after": after})
            valid += 1
        except ItemError as e:
            items.append({"product_id": str(pid), "name": prod.name, "sku": prod.sku, "status": "invalid", "error": str(e)})
            invalid += 1
    return {"operation": operation, "label": OPERATIONS[operation], "params": params, "selected": len(ids),
            "valid": valid, "invalid": invalid, "items": items,
            "run_mode": "background" if len(ids) > BACKGROUND_THRESHOLD else "inline"}


async def create(db: AsyncSession, business: Business, operation: str, params: dict, ids: list[uuid.UUID],
                 actor: Actor) -> BulkOperation:
    params = validate_params(operation, params)
    await load_context(db, business, operation, params, ids)  # validates the category etc.
    products = await owned_products(db, business.id, ids)
    op = BulkOperation(business_id=business.id, operation=operation, params=params, status="queued", total=len(ids),
                       created_by=actor.id, actor_label=actor.label,
                       run_mode="background" if len(ids) > BACKGROUND_THRESHOLD else "inline", result={})
    db.add(op)
    await db.flush()
    for pid in ids:
        prod = products.get(pid)
        db.add(BulkOperationItem(operation_id=op.id, business_id=business.id, product_id=prod.id if prod else None,
                                 product_name=prod.name if prod else None, status="pending",
                                 error=None if prod else "Product not found"))
    record(db, business.id, actor, "bulk.started", "bulk_operation", op.id,
           f"Bulk '{OPERATIONS[operation]}' started on {len(ids)} product(s)", {"operation": operation, "params": params},
           operation_id=op.id)
    await db.commit()
    return op


async def run(db: AsyncSession, op_id: uuid.UUID) -> BulkOperation:
    """Execute a queued bulk operation. Idempotent: only pending items are processed."""
    from app.services.validation.validator import revalidate_business

    op = await db.get(BulkOperation, op_id)
    if op is None or op.status not in ("queued", "running"):
        return op
    business = await db.get(Business, op.business_id)
    actor = Actor("user", op.created_by, op.actor_label) if op.created_by else Actor.system("Bulk operation")
    op.status, op.started_at = "running", op.started_at or now_utc()
    await db.commit()
    items = (await db.execute(select(BulkOperationItem).where(
        BulkOperationItem.operation_id == op.id, BulkOperationItem.status == "pending"))).scalars().all()
    ids = [i.product_id for i in items if i.product_id]
    bc = await load_context(db, business, op.operation, op.params, ids)
    for n, item in enumerate(items, 1):
        if item.product_id is None:
            item.status, item.processed_at = "failed", now_utc()
            op.failed += 1
        else:
            try:
                async with db.begin_nested():
                    prod = (await db.execute(select(Product).where(
                        Product.id == item.product_id, Product.business_id == op.business_id))).scalar_one_or_none()
                    if prod is None:
                        raise ItemError("Product not found")
                    before, after = plan(op.operation, prod, bc)
                    await apply_item(db, op.operation, prod, bc, actor, op.id)
                item.status, item.before, item.after = "succeeded", before, after
                op.succeeded += 1
            except ItemError as e:
                item.status, item.error = "failed", str(e)
                op.failed += 1
            except Exception as e:  # unexpected: record per item, keep going
                log.exception("bulk item failed")
                item.status, item.error = "failed", f"Unexpected error ({type(e).__name__})"
                op.failed += 1
            item.processed_at = now_utc()
        op.processed += 1
        if n % 25 == 0:
            await db.commit()  # real progress for pollers
    await db.flush()
    await revalidate_business(db, op.business_id)
    op.status = "completed" if op.failed == 0 else ("failed" if op.succeeded == 0 else "completed_with_errors")
    op.finished_at = now_utc()
    record(db, op.business_id, actor, "bulk.completed", "bulk_operation", op.id,
           f"Bulk '{OPERATIONS.get(op.operation, op.operation)}': {op.succeeded} succeeded, {op.failed} failed",
           {"succeeded": op.succeeded, "failed": op.failed}, operation_id=op.id)
    await db.commit()
    return op


def operation_dict(op: BulkOperation, items: list[BulkOperationItem] | None = None) -> dict:
    out = {
        "id": str(op.id), "operation": op.operation, "label": OPERATIONS.get(op.operation, op.operation),
        "params": op.params, "status": op.status, "total": op.total, "processed": op.processed,
        "succeeded": op.succeeded, "failed": op.failed, "skipped": op.skipped, "run_mode": op.run_mode,
        "result": op.result or {}, "error": op.error, "actor": op.actor_label,
        "created_at": aware(op.created_at).isoformat() if op.created_at else None,
        "started_at": aware(op.started_at).isoformat() if op.started_at else None,
        "finished_at": aware(op.finished_at).isoformat() if op.finished_at else None,
    }
    if items is not None:
        out["items"] = [{"id": str(i.id), "product_id": str(i.product_id) if i.product_id else None,
                         "name": i.product_name, "status": i.status, "before": i.before, "after": i.after,
                         "error": i.error} for i in items]
    return out


def dispatch(op_id: uuid.UUID) -> str:
    """Run a large bulk operation in the background (Celery or an in-process task)."""
    import asyncio

    from app.core.config import get_settings

    if get_settings().job_backend == "celery":
        from app.workers.celery_app import run_bulk_task

        run_bulk_task.delay(str(op_id))
        return "celery"
    from app.db.session import session_factory
    from app.workers.dispatch import _inline_tasks

    async def _go():
        async with session_factory()() as s:
            await run(s, op_id)

    task = asyncio.get_running_loop().create_task(_go())
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)
    return "inline"
