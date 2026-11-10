"""Variants, SKU rules, categories and attribute definitions."""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttributeDefinition, Business, Category, Product, ProductVariant
from app.services.commerce.audit import Actor, record
from app.services.commerce.money import aware, fmt, now_utc


class CatalogRuleError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


# ---------------------------------------------------------------- SKUs
async def sku_in_use(db: AsyncSession, business_id: uuid.UUID, sku: str | None, *, exclude_product: uuid.UUID | None = None,
                     exclude_variant: uuid.UUID | None = None) -> str | None:
    """Return a description of the item already using ``sku`` (case-insensitive), or None."""
    if not sku:
        return None
    key = sku.strip().lower()
    q = select(Product.id, Product.name).where(
        Product.business_id == business_id, Product.deleted_at.is_(None), func.lower(Product.sku) == key)
    if exclude_product:
        q = q.where(Product.id != exclude_product)
    hit = (await db.execute(q.limit(1))).first()
    if hit:
        return f"product '{hit.name}'"
    vq = select(ProductVariant.id, ProductVariant.name, Product.name.label("pname")).join(
        Product, Product.id == ProductVariant.product_id).where(
        Product.business_id == business_id, Product.deleted_at.is_(None), ProductVariant.archived_at.is_(None),
        func.lower(ProductVariant.sku) == key)
    if exclude_variant:
        vq = vq.where(ProductVariant.id != exclude_variant)
    hit = (await db.execute(vq.limit(1))).first()
    return f"variant '{hit.name}' of '{hit.pname}'" if hit else None


async def check_sku(db: AsyncSession, business: Business, sku: str | None, **exclude) -> None:
    if not business.enforce_unique_skus or not sku:
        return
    other = await sku_in_use(db, business.id, sku, **exclude)
    if other:
        raise CatalogRuleError(f"SKU '{sku}' is already used by {other}. SKUs must be unique in your catalog "
                               "(this rule can be changed in Settings).", "duplicate_sku", 409)


# ---------------------------------------------------------------- variants
def combination_key(options: dict[str, str]) -> str | None:
    clean = {re.sub(r"\s+", " ", str(k)).strip().lower(): re.sub(r"\s+", " ", str(v)).strip().lower()
             for k, v in (options or {}).items() if str(k).strip() and str(v).strip()}
    if not clean:
        return None
    return "|".join(f"{k}={v}" for k, v in sorted(clean.items()))[:600]


def variant_name(options: dict[str, str]) -> str:
    return " / ".join(str(v).strip() for v in options.values() if str(v).strip())


async def assert_unique_combination(db: AsyncSession, product_id: uuid.UUID, key: str | None,
                                    exclude: uuid.UUID | None = None) -> None:
    if key is None:
        return
    q = select(ProductVariant.id).where(ProductVariant.product_id == product_id, ProductVariant.combination_key == key,
                                        ProductVariant.archived_at.is_(None))
    if exclude:
        q = q.where(ProductVariant.id != exclude)
    if (await db.execute(q)).first():
        raise CatalogRuleError("A variant with these options already exists for this product", "duplicate_variant", 409)


def variant_dict(v: ProductVariant, p: Product, business: Business | None = None, include_private: bool = True) -> dict:
    from app.services.commerce.inventory import effective_threshold
    from app.services.commerce.pricing import variant_effective_price

    effective, status = variant_effective_price(v, p)
    out = {
        "id": str(v.id), "product_id": str(v.product_id), "name": v.name, "sku": v.sku, "options": v.attributes or {},
        "attributes": v.attributes or {},
        "price": float(v.price_amount) if v.price_amount is not None else None,  # Phase 1 shape
        "regular_price": fmt(v.price_amount), "sale_price": fmt(v.sale_price_amount),
        "inherits_price": v.price_amount is None, "effective_price": fmt(effective), "price_status": status,
        "currency": v.currency or p.currency, "availability": v.availability, "image_url": v.image_url,
        "position": v.position, "is_enabled": v.is_enabled, "origin": v.origin,
        "archived_at": aware(v.archived_at).isoformat() if v.archived_at else None,
        "stock_status": v.stock_status, "track_inventory": v.track_inventory,
        "created_at": aware(v.created_at).isoformat() if v.created_at else None,
        "updated_at": aware(v.updated_at).isoformat() if v.updated_at else None,
    }
    if include_private:
        out["inventory"] = {
            "on_hand": v.stock_on_hand, "reserved": v.stock_reserved, "available": v.stock_on_hand - v.stock_reserved,
            "low_stock_threshold": v.low_stock_threshold, "effective_low_stock_threshold": effective_threshold(v, business),
            "backorder_policy": v.backorder_policy, "location": v.inventory_location,
        }
    return out


# ---------------------------------------------------------------- categories
def path_key(path: str) -> str:
    return re.sub(r"\s+", " ", path).strip().lower()


async def category_path(db: AsyncSession, business_id: uuid.UUID, parent_id: uuid.UUID | None, name: str) -> str:
    if parent_id is None:
        return name
    parent = await get_category(db, business_id, parent_id)
    return f"{parent.path} / {name}"


async def get_category(db: AsyncSession, business_id: uuid.UUID, category_id) -> Category:
    try:
        cid = category_id if isinstance(category_id, uuid.UUID) else uuid.UUID(str(category_id))
    except ValueError:
        raise CatalogRuleError("Category not found", "not_found", 404)
    c = (await db.execute(select(Category).where(Category.id == cid, Category.business_id == business_id))).scalar_one_or_none()
    if c is None:
        raise CatalogRuleError("Category not found", "not_found", 404)
    return c


async def descendants(db: AsyncSession, business_id: uuid.UUID, category: Category) -> list[Category]:
    all_cats = (await db.execute(select(Category).where(Category.business_id == business_id))).scalars().all()
    children: dict[uuid.UUID | None, list[Category]] = {}
    for c in all_cats:
        children.setdefault(c.parent_id, []).append(c)
    out, stack = [], list(children.get(category.id, []))
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(children.get(c.id, []))
    return out


async def ensure_unique_path(db: AsyncSession, business_id: uuid.UUID, path: str, exclude: uuid.UUID | None = None) -> None:
    q = select(Category.id).where(Category.business_id == business_id, Category.path_key == path_key(path))
    if exclude:
        q = q.where(Category.id != exclude)
    if (await db.execute(q)).first():
        raise CatalogRuleError(f"A category named '{path}' already exists", "duplicate_category", 409)


async def rebuild_paths(db: AsyncSession, business_id: uuid.UUID, root: Category) -> int:
    """After a rename/move: recompute descendant paths and the category text on assigned products."""
    touched = [root, *await descendants(db, business_id, root)]
    by_id = {c.id: c for c in (await db.execute(select(Category).where(Category.business_id == business_id))).scalars()}
    for c in touched:  # parents come before children in this traversal order only for root; resolve iteratively
        parts, cur, seen = [], c, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            parts.append(cur.name)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        c.path = " / ".join(reversed(parts))
        c.path_key = path_key(c.path)
    await db.flush()
    n = 0
    for c in touched:
        res = await db.execute(update(Product).where(Product.business_id == business_id, Product.category_id == c.id)
                               .values(category=c.path[:300], category_inferred=False)
                               .execution_options(synchronize_session=False))
        n += res.rowcount or 0
    return n


async def assign_category(db: AsyncSession, p: Product, category: Category | None, actor: Actor,
                          operation_id=None, audit: bool = True) -> bool:
    old = p.category
    new_id = category.id if category else None
    if p.category_id == new_id and (category is None or p.category == category.path[:300]):
        return False
    p.category_id = new_id
    p.category = category.path[:300] if category else None
    p.category_inferred = False
    p.manual_overrides = sorted(set(p.manual_overrides or []) | {"category"})
    p.version += 1
    p.updated_at = now_utc()
    if audit:
        record(db, p.business_id, actor, "product.category_assigned", "product", p.id,
               f"Category of '{p.name}' changed: {old or 'none'} → {p.category or 'none'}",
               {"category": {"from": old, "to": p.category}}, operation_id=operation_id)
    return True


def category_dict(c: Category, counts: dict | None = None) -> dict:
    return {
        "id": str(c.id), "name": c.name, "path": c.path, "parent_id": str(c.parent_id) if c.parent_id else None,
        "description": c.description, "position": c.position,
        "archived_at": aware(c.archived_at).isoformat() if c.archived_at else None,
        "product_count": (counts or {}).get(c.id, 0),
        "created_at": aware(c.created_at).isoformat() if c.created_at else None,
    }


# ---------------------------------------------------------------- attributes
# Built-in canonical names for equivalent attribute keys (merchant definitions can add more aliases).
BUILTIN_ALIASES = {
    "product_weight": "weight", "item_weight": "weight", "net_weight": "weight", "shipping_weight": "weight",
    "brand_name": "brand", "make": "brand",
    "manufacturer_name": "manufacturer", "mfr": "manufacturer",
    "stock": "stock_quantity", "quantity_available": "stock_quantity", "qty_available": "stock_quantity",
    "stock_qty": "stock_quantity", "quantity_in_stock": "stock_quantity",
    "colour": "color", "product_color": "color", "product_colour": "color",
    "mpn_number": "mpn", "manufacturer_part_number": "mpn", "part_number": "mpn",
    "ean": "gtin", "upc": "gtin", "barcode": "gtin",
    "dimension": "dimensions", "size_dimensions": "dimensions",
}
# canonical keys that correspond to product columns (filled only when the column is empty)
COLUMN_KEYS = {"brand", "manufacturer", "material", "mpn"}
DATA_TYPES = ("string", "number", "boolean", "date", "enum")


def canonical_key(key: str, merchant_aliases: dict[str, str] | None = None) -> str:
    k = (key or "").strip().lower()
    if merchant_aliases and k in merchant_aliases:
        return merchant_aliases[k]
    return BUILTIN_ALIASES.get(k, k)


async def merchant_alias_map(db: AsyncSession, business_id: uuid.UUID) -> dict[str, str]:
    rows = (await db.execute(select(AttributeDefinition).where(
        AttributeDefinition.business_id == business_id, AttributeDefinition.archived_at.is_(None)))).scalars().all()
    out = {}
    for d in rows:
        for a in d.aliases or []:
            out[str(a).strip().lower()] = d.key
    return out


def check_value(d: AttributeDefinition, value: str | None) -> str | None:
    """Return a problem description if ``value`` does not match the definition's type, else None."""
    if value in (None, ""):
        return None
    v = str(value).strip()
    if d.data_type == "number":
        try:
            Decimal(v.replace(",", ""))
        except InvalidOperation:
            return f"'{v}' is not a number"
    elif d.data_type == "boolean":
        if v.lower() not in ("true", "false", "yes", "no", "1", "0"):
            return f"'{v}' is not yes/no"
    elif d.data_type == "date":
        try:
            date.fromisoformat(v[:10])
        except ValueError:
            return f"'{v}' is not a date (YYYY-MM-DD)"
    elif d.data_type == "enum":
        allowed = [str(a).lower() for a in d.allowed_values or []]
        if allowed and v.lower() not in allowed:
            return f"'{v}' is not one of: {', '.join(d.allowed_values)}"
    return None


def definition_dict(d: AttributeDefinition) -> dict:
    return {
        "id": str(d.id), "key": d.key, "name": d.name, "data_type": d.data_type, "description": d.description,
        "unit": d.unit, "is_required": d.is_required, "allowed_values": d.allowed_values or [], "aliases": d.aliases or [],
        "category_id": str(d.category_id) if d.category_id else None,
        "archived_at": aware(d.archived_at).isoformat() if d.archived_at else None,
    }


async def definitions_for(db: AsyncSession, business_id: uuid.UUID) -> list[AttributeDefinition]:
    return list((await db.execute(select(AttributeDefinition).where(
        AttributeDefinition.business_id == business_id, AttributeDefinition.archived_at.is_(None)))).scalars().all())


def applicable(definitions: list[AttributeDefinition], category_ids: set[uuid.UUID]) -> list[AttributeDefinition]:
    return [d for d in definitions if d.category_id is None or d.category_id in category_ids]


async def ancestor_ids(db: AsyncSession, business_id: uuid.UUID, category_id: uuid.UUID | None,
                       cache: dict | None = None) -> set[uuid.UUID]:
    if category_id is None:
        return set()
    if cache is None:
        cache = {c.id: c for c in (await db.execute(select(Category).where(Category.business_id == business_id))).scalars()}
    out, cur = set(), cache.get(category_id)
    while cur is not None and cur.id not in out:
        out.add(cur.id)
        cur = cache.get(cur.parent_id) if cur.parent_id else None
    return out


def sku_filter(q: str):
    return or_(func.lower(Product.sku) == q.lower(), func.lower(ProductVariant.sku) == q.lower())
