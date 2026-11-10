"""Categories, attribute definitions, variants, bulk operations, audit logs, API keys."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_SCOPES, AuthContext, current_auth
from app.api.v1.products import contains_pattern, get_owned_product
from app.core.errors import AppError, not_found
from app.core.security import generate_opaque_token, hash_opaque_token
from app.db.session import get_db
from app.models import (
    ApiKey,
    ApiKeyUsage,
    AttributeDefinition,
    AuditLog,
    BulkOperation,
    BulkOperationItem,
    Category,
    Product,
    ProductVariant,
    live_products,
)
from app.schemas.commerce import (
    ApiKeyIn,
    AttributeDefinitionIn,
    AttributeDefinitionUpdate,
    BulkIn,
    CatalogRulesIn,
    CategoryIn,
    CategoryUpdate,
    VariantIn,
    VariantUpdate,
)
from app.services.commerce import bulk
from app.services.commerce.audit import actor_from_ctx, record
from app.services.commerce.catalog_rules import (
    CatalogRuleError,
    assert_unique_combination,
    category_dict,
    category_path,
    check_sku,
    combination_key,
    definition_dict,
    descendants,
    ensure_unique_path,
    get_category,
    path_key,
    rebuild_paths,
    variant_dict,
    variant_name,
)
from app.services.commerce.inventory import change_stock, refresh_product_status
from app.services.commerce.money import aware, now_utc
from app.services.commerce.pricing import set_pricing
from app.services.normalization.normalize import attribute_key
from app.services.validation.validator import revalidate_business

router = APIRouter(tags=["catalog management"])


def _uuid(v: str | None, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        raise not_found(what)


# ================================================================ categories
async def _counts(db: AsyncSession, business_id) -> dict:
    return dict((await db.execute(select(Product.category_id, func.count()).where(
        *live_products(business_id), Product.category_id.is_not(None)).group_by(Product.category_id))).all())


@router.get("/categories")
async def list_categories(include_archived: bool = False, ctx: AuthContext = Depends(current_auth),
                          db: AsyncSession = Depends(get_db)):
    q = select(Category).where(Category.business_id == ctx.business_id)
    if not include_archived:
        q = q.where(Category.archived_at.is_(None))
    cats = (await db.execute(q.order_by(Category.path_key))).scalars().all()
    counts = await _counts(db, ctx.business_id)
    uncategorised = (await db.execute(select(func.count()).select_from(Product).where(
        *live_products(ctx.business_id), Product.category_id.is_(None)))).scalar_one()
    unmapped = (await db.execute(select(Product.category, func.count()).where(
        *live_products(ctx.business_id), Product.category_id.is_(None), Product.category.is_not(None))
        .group_by(Product.category).order_by(func.count().desc()).limit(100))).all()
    return {"items": [category_dict(c, counts) for c in cats], "uncategorised": uncategorised,
            "unmapped_texts": [{"category": c, "count": n} for c, n in unmapped]}


@router.post("/categories", status_code=201)
async def create_category(body: CategoryIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    parent_id = _uuid(body.parent_id, "Parent category") if body.parent_id else None
    if parent_id:
        parent = await get_category(db, ctx.business_id, parent_id)
        if parent.archived_at is not None:
            raise AppError(409, "parent_archived", "The parent category is archived")
    path = await category_path(db, ctx.business_id, parent_id, body.name)
    await ensure_unique_path(db, ctx.business_id, path)
    c = Category(business_id=ctx.business_id, parent_id=parent_id, name=body.name, path=path, path_key=path_key(path),
                 description=body.description, position=body.position)
    db.add(c)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppError(409, "duplicate_category", f"A category named '{path}' already exists")
    record(db, ctx.business_id, actor_from_ctx(ctx), "category.created", "category", c.id, f"Category '{path}' created")
    await db.commit()
    return category_dict(c)


@router.post("/categories/import-from-products")
async def import_categories(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Create managed categories from the category texts found on products and link those products."""
    actor = actor_from_ctx(ctx)
    texts = (await db.execute(select(Product.category).where(
        *live_products(ctx.business_id), Product.category_id.is_(None), Product.category.is_not(None)).distinct())).scalars().all()
    created = linked = 0
    for text in texts:
        parts = [x.strip() for x in text.replace(">", "/").split("/") if x.strip()][:6]
        parent, path = None, None
        for name in parts:
            path = f"{path} / {name}" if path else name
            cat = (await db.execute(select(Category).where(Category.business_id == ctx.business_id,
                                                           Category.path_key == path_key(path)))).scalar_one_or_none()
            if cat is None:
                cat = Category(business_id=ctx.business_id, parent_id=parent.id if parent else None, name=name[:200],
                               path=path, path_key=path_key(path))
                db.add(cat)
                await db.flush()
                created += 1
            parent = cat
        if parent is None:
            continue
        rows = (await db.execute(select(Product).where(*live_products(ctx.business_id), Product.category == text,
                                                       Product.category_id.is_(None)))).scalars().all()
        for p in rows:
            p.category_id = parent.id  # same text; not a merchant override (the website still says the same)
            linked += 1
    record(db, ctx.business_id, actor, "category.imported", "category", None,
           f"Created {created} categories from product data and linked {linked} products")
    await db.commit()
    return {"created": created, "linked": linked}


@router.patch("/categories/{category_id}")
async def update_category(category_id: str, body: CategoryUpdate, ctx: AuthContext = Depends(current_auth),
                          db: AsyncSession = Depends(get_db)):
    c = await get_category(db, ctx.business_id, category_id)
    actor = actor_from_ctx(ctx)
    fields = body.model_fields_set
    changes = {}
    if "parent_id" in fields:
        new_parent = _uuid(body.parent_id, "Parent category") if body.parent_id else None
        if new_parent == c.id or (new_parent and new_parent in {d.id for d in await descendants(db, ctx.business_id, c)}):
            raise AppError(422, "category_cycle", "A category cannot be moved inside itself or its subcategories")
        if new_parent:
            await get_category(db, ctx.business_id, new_parent)
        changes["parent_id"] = {"from": str(c.parent_id) if c.parent_id else None, "to": str(new_parent) if new_parent else None}
        c.parent_id = new_parent
    if body.name is not None and body.name != c.name:
        changes["name"] = {"from": c.name, "to": body.name}
        c.name = body.name
    if "parent_id" in fields or "name" in changes:
        path = await category_path(db, ctx.business_id, c.parent_id, c.name)
        await ensure_unique_path(db, ctx.business_id, path, exclude=c.id)
    for f in ("description", "position"):
        if f in fields and getattr(body, f) is not None:
            setattr(c, f, getattr(body, f))
    if body.archived is not None:
        c.archived_at = now_utc() if body.archived else None
        changes["archived"] = body.archived
    await db.flush()
    moved = await rebuild_paths(db, ctx.business_id, c) if ("parent_id" in fields or "name" in changes) else 0
    record(db, ctx.business_id, actor, "category.updated", "category", c.id,
           f"Category '{c.path}' updated" + (f" ({moved} product(s) relabelled)" if moved else ""), changes)
    await db.commit()
    return category_dict(c, await _counts(db, ctx.business_id))


@router.delete("/categories/{category_id}", status_code=204)
async def delete_category(category_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    c = await get_category(db, ctx.business_id, category_id)
    in_use = (await db.execute(select(func.count()).select_from(Product).where(
        Product.business_id == ctx.business_id, Product.category_id == c.id, Product.deleted_at.is_(None)))).scalar_one()
    children = (await db.execute(select(func.count()).select_from(Category).where(Category.parent_id == c.id))).scalar_one()
    if in_use or children:
        raise AppError(409, "category_in_use",
                       f"'{c.path}' has {in_use} product(s) and {children} subcategor{'y' if children == 1 else 'ies'}. "
                       "Move them first, or archive the category instead.")
    record(db, ctx.business_id, actor_from_ctx(ctx), "category.deleted", "category", c.id, f"Category '{c.path}' deleted")
    await db.delete(c)
    await db.commit()
    return Response(status_code=204)


# ================================================================ attribute definitions
@router.get("/attribute-definitions")
async def list_definitions(include_archived: bool = False, ctx: AuthContext = Depends(current_auth),
                           db: AsyncSession = Depends(get_db)):
    q = select(AttributeDefinition).where(AttributeDefinition.business_id == ctx.business_id)
    if not include_archived:
        q = q.where(AttributeDefinition.archived_at.is_(None))
    rows = (await db.execute(q.order_by(AttributeDefinition.name))).scalars().all()
    # how many live products carry each key
    from app.models import ProductAttribute

    usage = dict((await db.execute(select(ProductAttribute.key, func.count(func.distinct(ProductAttribute.product_id)))
                                   .join(Product, Product.id == ProductAttribute.product_id)
                                   .where(*live_products(ctx.business_id)).group_by(ProductAttribute.key))).all())
    return {"items": [{**definition_dict(d), "products_with_value": usage.get(d.key, 0)} for d in rows],
            "observed_keys": sorted(({"key": k, "count": n} for k, n in usage.items()), key=lambda x: -x["count"])[:100]}


def _check_definition(data_type: str, allowed: list[str]) -> None:
    if data_type == "enum" and not allowed:
        raise AppError(422, "allowed_values_required", "A controlled-list attribute needs at least one allowed value")


@router.post("/attribute-definitions", status_code=201)
async def create_definition(body: AttributeDefinitionIn, ctx: AuthContext = Depends(current_auth),
                            db: AsyncSession = Depends(get_db)):
    _check_definition(body.data_type, body.allowed_values)
    key = body.key or attribute_key(body.name)
    cat_id = (await get_category(db, ctx.business_id, body.category_id)).id if body.category_id else None
    d = AttributeDefinition(business_id=ctx.business_id, key=key, name=body.name, data_type=body.data_type,
                            description=body.description, unit=body.unit, is_required=body.is_required,
                            allowed_values=[v.strip() for v in body.allowed_values if v.strip()],
                            aliases=sorted({attribute_key(a) for a in body.aliases if a.strip()} - {key}), category_id=cat_id)
    db.add(d)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppError(409, "duplicate_attribute", f"An attribute with key '{key}' already exists")
    record(db, ctx.business_id, actor_from_ctx(ctx), "attribute.created", "attribute_definition", d.id,
           f"Attribute '{d.name}' ({d.data_type}) defined")
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    return definition_dict(d)


@router.patch("/attribute-definitions/{definition_id}")
async def update_definition(definition_id: str, body: AttributeDefinitionUpdate, ctx: AuthContext = Depends(current_auth),
                            db: AsyncSession = Depends(get_db)):
    d = (await db.execute(select(AttributeDefinition).where(
        AttributeDefinition.id == _uuid(definition_id, "Attribute"),
        AttributeDefinition.business_id == ctx.business_id))).scalar_one_or_none()
    if d is None:
        raise not_found("Attribute")
    for f in body.model_fields_set - {"archived", "category_id", "aliases"}:
        setattr(d, f, getattr(body, f))
    if "aliases" in body.model_fields_set:
        d.aliases = sorted({attribute_key(a) for a in body.aliases or [] if a.strip()} - {d.key})
    if "category_id" in body.model_fields_set:
        d.category_id = (await get_category(db, ctx.business_id, body.category_id)).id if body.category_id else None
    if body.archived is not None:
        d.archived_at = now_utc() if body.archived else None
    _check_definition(d.data_type, d.allowed_values or [])
    record(db, ctx.business_id, actor_from_ctx(ctx), "attribute.updated", "attribute_definition", d.id,
           f"Attribute '{d.name}' updated", {f: str(getattr(body, f)) for f in body.model_fields_set})
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    return definition_dict(d)


# ================================================================ variants
async def _owned_variant(db: AsyncSession, ctx: AuthContext, variant_id: str) -> tuple[ProductVariant, Product]:
    v = await db.get(ProductVariant, _uuid(variant_id, "Variant"))
    if v is None:
        raise not_found("Variant")
    p = await db.get(Product, v.product_id)
    if p is None or p.business_id != ctx.business_id or p.deleted_at is not None:
        raise not_found("Variant")
    return v, p


@router.get("/products/{product_id}/variants")
async def list_variants(product_id: str, include_archived: bool = False, ctx: AuthContext = Depends(current_auth),
                        db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    vs = [v for v in p.variants if include_archived or v.archived_at is None]
    return {"items": [variant_dict(v, p, ctx.business) for v in sorted(vs, key=lambda v: (v.position, v.name or ""))]}


@router.get("/variants")
async def all_variants(q: str | None = None, stock_status: str | None = None, page: int = Query(1, ge=1),
                       page_size: int = Query(50, ge=1, le=200), ctx: AuthContext = Depends(current_auth),
                       db: AsyncSession = Depends(get_db)):
    stmt = select(ProductVariant, Product).join(Product, Product.id == ProductVariant.product_id).where(
        *live_products(ctx.business_id), ProductVariant.archived_at.is_(None))
    if q:
        like = contains_pattern(q)
        stmt = stmt.where(or_(func.lower(ProductVariant.sku).like(like, escape="\\"),
                              func.lower(ProductVariant.name).like(like, escape="\\"),
                              func.lower(Product.name).like(like, escape="\\")))
    if stock_status:
        stmt = stmt.where(ProductVariant.stock_status.in_(stock_status.split(",")))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Product.name, ProductVariant.position, ProductVariant.name)
                             .offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [{**variant_dict(v, p, ctx.business), "product_name": p.name, "product_sku": p.sku} for v, p in rows],
            "total": total, "page": page, "page_size": page_size}


@router.post("/products/{product_id}/variants", status_code=201)
async def create_variant(product_id: str, body: VariantIn, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    actor = actor_from_ctx(ctx)
    if not body.options and not body.name:
        raise AppError(422, "options_required", "Give the variant at least one option (e.g. Size: M) or a name")
    key = combination_key(body.options)
    await assert_unique_combination(db, p.id, key)
    await check_sku(db, ctx.business, body.sku)
    v = ProductVariant(product_id=p.id, business_id=ctx.business_id, name=body.name or variant_name(body.options),
                       sku=body.sku, attributes=body.options, combination_key=key, image_url=body.image_url,
                       is_enabled=body.is_enabled, origin="merchant",
                       position=body.position if body.position is not None else len([x for x in p.variants if x.archived_at is None]),
                       track_inventory=body.track_inventory, low_stock_threshold=body.low_stock_threshold,
                       backorder_policy=body.backorder_policy)
    p.variants.append(v)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppError(409, "duplicate_variant", "A variant with these options already exists for this product")
    if body.regular_price is not None or body.sale_price is not None:
        await set_pricing(db, p, actor=actor, variant=v, regular=body.regular_price, sale=body.sale_price,
                          change_type="product_created", audit=False)
    p.manual_overrides = sorted(set(p.manual_overrides or []) | {"variants"})
    if body.track_inventory:
        await refresh_product_status(db, p, ctx.business)
        if body.stock_quantity is not None:
            await change_stock(db, p, actor=actor, variant=v, operation="set", quantity=body.stock_quantity,
                               reason="Initial stock", transaction_type="initial")
    p.version += 1
    p.updated_at = now_utc()
    record(db, ctx.business_id, actor, "variant.created", "product", p.id,
           f"Variant '{v.name}' added to '{p.name}'", {"variant_id": v.id, "options": body.options, "sku": body.sku})
    await db.commit()
    await db.refresh(v)
    return variant_dict(v, p, ctx.business)


@router.patch("/variants/{variant_id}")
async def update_variant(variant_id: str, body: VariantUpdate, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    v, p = await _owned_variant(db, ctx, variant_id)
    actor = actor_from_ctx(ctx)
    fields = body.model_fields_set
    changes = {}
    if body.archived is not None:
        if body.archived and v.archived_at is None:
            v.archived_at = now_utc()
            changes["archived"] = True
        elif not body.archived and v.archived_at is not None:
            await assert_unique_combination(db, p.id, v.combination_key, exclude=v.id)
            v.archived_at = None
            changes["archived"] = False
    if v.archived_at is not None and fields - {"archived"}:
        raise AppError(409, "variant_archived", "Restore the variant before editing it")
    if "options" in fields and body.options is not None:
        key = combination_key(body.options)
        await assert_unique_combination(db, p.id, key, exclude=v.id)
        changes["options"] = {"from": v.attributes, "to": body.options}
        v.attributes, v.combination_key = body.options, key
        if "name" not in fields:
            v.name = variant_name(body.options) or v.name
    if "sku" in fields and body.sku != v.sku:
        await check_sku(db, ctx.business, body.sku, exclude_variant=v.id)
        changes["sku"] = {"from": v.sku, "to": body.sku}
        v.sku = body.sku
    for f in ("name", "image_url", "is_enabled", "position"):
        if f in fields and getattr(body, f) is not None and getattr(v, f) != getattr(body, f):
            changes[f] = {"from": getattr(v, f), "to": getattr(body, f)}
            setattr(v, f, getattr(body, f))
    price_kwargs = {}
    if body.inherit_price:
        price_kwargs.update(regular=None, sale=None)
    else:
        if "regular_price" in fields:
            price_kwargs["regular"] = body.regular_price
        if "sale_price" in fields:
            price_kwargs["sale"] = body.sale_price
    if price_kwargs:
        await set_pricing(db, p, actor=actor, variant=v, **price_kwargs)
    v.origin = "merchant" if v.origin == "extracted" else v.origin
    p.manual_overrides = sorted(set(p.manual_overrides or []) | {"variants"})
    await db.flush()
    await refresh_product_status(db, p, ctx.business)
    p.version += 1
    p.updated_at = now_utc()
    if changes:
        record(db, ctx.business_id, actor, "variant.archived" if changes.get("archived") else "variant.updated", "product", p.id,
               f"Variant '{v.name}' of '{p.name}' {'archived' if changes.get('archived') else 'updated'}",
               {"variant_id": v.id, **changes})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError(409, "duplicate_variant", "A variant with these options already exists for this product")
    await db.refresh(v)
    return variant_dict(v, p, ctx.business)


@router.delete("/variants/{variant_id}", status_code=204)
async def archive_variant(variant_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Variants are archived, never hard-deleted, so their price and stock history is preserved."""
    v, p = await _owned_variant(db, ctx, variant_id)
    if v.archived_at is None:
        v.archived_at = now_utc()
        p.manual_overrides = sorted(set(p.manual_overrides or []) | {"variants"})
        await db.flush()
        await refresh_product_status(db, p, ctx.business)
        record(db, ctx.business_id, actor_from_ctx(ctx), "variant.archived", "product", p.id,
               f"Variant '{v.name}' of '{p.name}' archived", {"variant_id": v.id})
        await db.commit()
    return Response(status_code=204)


# ================================================================ bulk operations
async def run_bulk_request(db: AsyncSession, ctx: AuthContext, operation: str, product_ids: list[str], params: dict,
                           dry_run: bool) -> dict:
    try:
        ids = list(dict.fromkeys(uuid.UUID(i) for i in product_ids))
    except ValueError:
        raise AppError(422, "invalid_id", "Invalid product id")
    try:
        if dry_run:
            return {"preview": await bulk.preview(db, ctx.business, operation, params, ids)}
        op = await bulk.create(db, ctx.business, operation, params, ids, actor_from_ctx(ctx))
    except ValueError as e:
        raise AppError(422, "invalid_bulk_operation", str(e))
    if op.run_mode == "background":
        bulk.dispatch(op.id)
        return {"operation": bulk.operation_dict(op)}
    op = await bulk.run(db, op.id)
    items = (await db.execute(select(BulkOperationItem).where(BulkOperationItem.operation_id == op.id))).scalars().all()
    return {"operation": bulk.operation_dict(op, items)}


@router.get("/bulk-operations/catalog")
async def bulk_catalog():
    return {"operations": [{"key": k, "label": v} for k, v in bulk.OPERATIONS.items()],
            "background_threshold": bulk.BACKGROUND_THRESHOLD}


@router.post("/bulk-operations")
async def create_bulk(body: BulkIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Preview (dry_run=true) or execute a bulk operation. Large operations run in the background; poll GET /{id}."""
    return await run_bulk_request(db, ctx, body.operation, body.product_ids, body.params, body.dry_run)


@router.get("/bulk-operations")
async def list_bulk(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                    ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    q = select(BulkOperation).where(BulkOperation.business_id == ctx.business_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(BulkOperation.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [bulk.operation_dict(o) for o in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/bulk-operations/{operation_id}")
async def get_bulk(operation_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    op = (await db.execute(select(BulkOperation).where(BulkOperation.id == _uuid(operation_id, "Bulk operation"),
                                                       BulkOperation.business_id == ctx.business_id))).scalar_one_or_none()
    if op is None:
        raise not_found("Bulk operation")
    items = (await db.execute(select(BulkOperationItem).where(BulkOperationItem.operation_id == op.id)
                              .order_by(BulkOperationItem.status, BulkOperationItem.product_name))).scalars().all()
    return bulk.operation_dict(op, items)


# ================================================================ audit logs
def audit_dict(a: AuditLog) -> dict:
    return {"id": str(a.id), "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
            "summary": a.summary, "changes": a.changes or {}, "actor_type": a.actor_type, "actor_id": a.actor_id,
            "actor": a.actor_label, "request_id": a.request_id, "operation_id": a.operation_id,
            "created_at": aware(a.created_at).isoformat()}


@router.get("/audit-logs")
async def audit_logs(action: str | None = None, entity_type: str | None = None, entity_id: str | None = None,
                     actor_type: str | None = None, q: str | None = None, since: datetime | None = None,
                     until: datetime | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                     ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    stmt = select(AuditLog).where(AuditLog.business_id == ctx.business_id)
    if action:
        stmt = stmt.where(or_(*(AuditLog.action.like(f"{a.replace('%', '')}%") for a in action.split(","))))
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor_type:
        stmt = stmt.where(AuditLog.actor_type.in_(actor_type.split(",")))
    if q:
        stmt = stmt.where(func.lower(AuditLog.summary).like(contains_pattern(q), escape="\\"))
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(AuditLog.created_at.desc(), AuditLog.id).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    actions = (await db.execute(select(AuditLog.action).where(AuditLog.business_id == ctx.business_id).distinct())).scalars().all()
    return {"items": [audit_dict(a) for a in rows], "total": total, "page": page, "page_size": page_size,
            "actions": sorted(actions)}


# ================================================================ API keys
def key_dict(k: ApiKey) -> dict:
    return {"id": str(k.id), "name": k.name, "prefix": k.prefix, "scopes": k.scopes or [],
            "rate_limit_per_minute": k.rate_limit_per_minute, "usage_count": k.usage_count,
            "last_used_at": aware(k.last_used_at).isoformat() if k.last_used_at else None,
            "revoked_at": aware(k.revoked_at).isoformat() if k.revoked_at else None,
            "expires_at": aware(k.expires_at).isoformat() if k.expires_at else None,
            "rotated_from_id": str(k.rotated_from_id) if k.rotated_from_id else None,
            "created_at": aware(k.created_at).isoformat() if k.created_at else None,
            "active": k.revoked_at is None and (k.expires_at is None or aware(k.expires_at) > now_utc())}


async def issue_key(db: AsyncSession, ctx: AuthContext, name: str, scopes: list[str], rate: int, expires_at=None,
                    rotated_from: ApiKey | None = None, legacy: bool = False) -> tuple[ApiKey, str]:
    secret = generate_opaque_token("ar_cat_")
    k = ApiKey(business_id=ctx.business_id, name=name, prefix=secret[:12], key_hash=hash_opaque_token(secret),
               scopes=[s for s in scopes if s in ALL_SCOPES], rate_limit_per_minute=rate, expires_at=expires_at,
               created_by=str(ctx.user.id) if ctx.user else None, rotated_from_id=rotated_from.id if rotated_from else None,
               is_legacy_default=legacy)
    db.add(k)
    await db.flush()
    return k, secret


@router.get("/api-keys")
async def list_keys(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    keys = (await db.execute(select(ApiKey).where(ApiKey.business_id == ctx.business_id)
                             .order_by(ApiKey.revoked_at.is_not(None), ApiKey.created_at.desc()))).scalars().all()
    usage = (await db.execute(select(ApiKeyUsage.api_key_id, ApiKeyUsage.day, ApiKeyUsage.requests).where(
        ApiKeyUsage.business_id == ctx.business_id).order_by(ApiKeyUsage.day.desc()).limit(500))).all()
    per_key: dict = {}
    for kid, day, n in usage:
        per_key.setdefault(kid, []).append({"day": day, "requests": n})
    return {"items": [{**key_dict(k), "daily_usage": per_key.get(k.id, [])[:30]} for k in keys],
            "scopes": {"catalog:read": "Published products, categories and feeds",
                       "pricing:read": "Selling prices (cost prices are never exposed)",
                       "inventory:read": "Available stock quantities (reserved stock and locations are never exposed)",
                       "evidence:read": "Source evidence (where each value came from)",
                       "agent:read": "Agent API and MCP tools (/api/v3/agents): search, details, compare, pricing, availability",
                       "checkout:write": "Create validated carts and merchant checkout hand-offs (no payment is processed)"}}


@router.post("/api-keys", status_code=201)
async def create_key(body: ApiKeyIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    k, secret = await issue_key(db, ctx, body.name, body.scopes, body.rate_limit_per_minute, body.expires_at)
    record(db, ctx.business_id, actor_from_ctx(ctx), "api_key.created", "api_key", k.id,
           f"API key '{k.name}' created ({', '.join(k.scopes)})", {"prefix": k.prefix, "scopes": k.scopes})
    await db.commit()
    return {**key_dict(k), "key": secret, "note": "Copy this key now. It is stored hashed and cannot be shown again."}


async def _owned_key(db, ctx, key_id) -> ApiKey:
    k = (await db.execute(select(ApiKey).where(ApiKey.id == _uuid(key_id, "API key"),
                                               ApiKey.business_id == ctx.business_id))).scalar_one_or_none()
    if k is None:
        raise not_found("API key")
    return k


@router.post("/api-keys/{key_id}/rotate", status_code=201)
async def rotate_key(key_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    old = await _owned_key(db, ctx, key_id)
    if old.revoked_at is not None:
        raise AppError(409, "revoked", "This key is revoked; create a new key instead")
    k, secret = await issue_key(db, ctx, old.name, old.scopes or [], old.rate_limit_per_minute, old.expires_at,
                                rotated_from=old, legacy=old.is_legacy_default)
    old.revoked_at = now_utc()
    record(db, ctx.business_id, actor_from_ctx(ctx), "api_key.rotated", "api_key", k.id,
           f"API key '{k.name}' rotated ({old.prefix}… revoked, {k.prefix}… issued)", {"old_prefix": old.prefix, "prefix": k.prefix})
    await db.commit()
    return {**key_dict(k), "key": secret, "note": "The previous key stopped working immediately."}


@router.post("/api-keys/{key_id}/revoke")
async def revoke_key(key_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    k = await _owned_key(db, ctx, key_id)
    if k.revoked_at is None:
        k.revoked_at = now_utc()
        record(db, ctx.business_id, actor_from_ctx(ctx), "api_key.revoked", "api_key", k.id,
               f"API key '{k.name}' ({k.prefix}…) revoked", {"prefix": k.prefix})
        await db.commit()
    return key_dict(k)


# ================================================================ catalog rules
@router.get("/settings/catalog-rules")
async def get_rules(ctx: AuthContext = Depends(current_auth)):
    b = ctx.business
    return {"enforce_unique_skus": b.enforce_unique_skus, "default_low_stock_threshold": b.default_low_stock_threshold}


@router.patch("/settings/catalog-rules")
async def update_rules(body: CatalogRulesIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    b = ctx.business
    changes = {}
    for f in body.model_fields_set:
        v = getattr(body, f)
        if v is not None and getattr(b, f) != v:
            changes[f] = {"from": getattr(b, f), "to": v}
            setattr(b, f, v)
    if changes:
        record(db, b.id, actor_from_ctx(ctx), "settings.catalog_rules_updated", "business", b.id, "Catalog rules updated", changes)
        if "default_low_stock_threshold" in changes:
            from app.services.commerce.inventory import effective_threshold, stock_status

            for p in (await db.execute(select(Product).where(*live_products(b.id), Product.track_inventory.is_(True)))).scalars():
                p.stock_status = stock_status(True, p.stock_on_hand, p.stock_reserved, effective_threshold(p, b), p.backorder_policy)
        await db.commit()
    return {"enforce_unique_skus": b.enforce_unique_skus, "default_low_stock_threshold": b.default_low_stock_threshold}
