"""Integration synchronization: import from a store, push AgentReady changes back, webhooks.

Safety rules:
* imports never overwrite merchant-managed values (``manual_overrides``): differences become SyncConflicts;
* every sync is recorded as a SyncJob with direction and source; price/stock changes write history;
* webhooks are verified (HMAC), de-duplicated by delivery id, and only act on real differences — an
  echo of AgentReady's own push carries no difference, so pushes cannot loop;
* each product is processed in its own savepoint so one bad record does not abort the sync.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Business,
    Category,
    CommerceIntegration,
    IntegrationLink,
    IntegrationWebhookEvent,
    Product,
    ProductAttribute,
    ProductEvidence,
    ProductVariant,
    SyncConflict,
    SyncJob,
)
from app.services.commerce.audit import Actor, jsonable, record
from app.services.commerce.catalog_rules import combination_key, path_key
from app.services.commerce.inventory import InventoryError, change_stock, refresh_product_status
from app.services.commerce.money import aware, fmt, now_utc
from app.services.commerce.pricing import set_pricing
from app.services.integrations.crypto import CredentialError, decrypt_json, encrypt_json
from app.services.integrations.http import IntegrationHTTPError
from app.services.integrations.providers import (
    PROVIDERS,
    ExternalProduct,
    ExternalVariant,
    ProviderError,
    _money,
    build,
)
from app.services.normalization.normalize import attribute_key

log = logging.getLogger("agentready.integrations")
SYNC_ERRORS = (ProviderError, IntegrationHTTPError, CredentialError)


def integration_dict(i: CommerceIntegration) -> dict:
    """Never includes credentials or secrets."""
    return {
        "id": str(i.id), "provider": i.provider, "name": i.name, "store_url": i.store_url, "status": i.status,
        "credentials_hint": i.credentials_hint, "has_webhook_secret": bool(i.webhook_secret_encrypted),
        "settings": i.settings or {}, "currency": i.currency, "last_error": i.last_error,
        "last_verified_at": aware(i.last_verified_at).isoformat() if i.last_verified_at else None,
        "last_sync_at": aware(i.last_sync_at).isoformat() if i.last_sync_at else None,
        "created_at": aware(i.created_at).isoformat() if i.created_at else None,
        "webhook_path": f"/api/v1/integrations/{i.id}/webhooks",
    }


def provider_for(i: CommerceIntegration, client=None):
    return build(i.provider, i.store_url, decrypt_json(i.credentials_encrypted), client)


def new_webhook_secret(i: CommerceIntegration) -> str:
    secret = secrets.token_urlsafe(32)
    i.webhook_secret_encrypted = encrypt_json({"secret": secret})
    return secret


async def verify_integration(db: AsyncSession, i: CommerceIntegration, actor: Actor, client=None) -> dict:
    provider = None
    try:
        provider = provider_for(i, client)
        info = await provider.verify()
        i.status, i.last_error, i.last_verified_at = "connected", None, now_utc()
        if info.get("currency"):
            i.currency = info["currency"]
        record(db, i.business_id, actor, "integration.verified", "integration", i.id,
               f"{i.provider.title()} connection verified ({i.store_url})")
        return {"ok": True, **info}
    except SYNC_ERRORS as e:
        i.status, i.last_error = "error", str(e)
        return {"ok": False, "message": str(e)}
    finally:
        if provider is not None:
            await provider.aclose()


# ---------------------------------------------------------------- import
async def _category_for(db: AsyncSession, business_id: uuid.UUID, name: str | None) -> Category | None:
    if not name:
        return None
    return (await db.execute(select(Category).where(Category.business_id == business_id,
                                                    Category.path_key == path_key(name)))).scalar_one_or_none()


def _conflict(db, i: CommerceIntegration, job: SyncJob, p: Product, field: str, external, current) -> None:
    db.add(SyncConflict(business_id=p.business_id, product_id=p.id, sync_job_id=job.id, source_type=i.provider,
                        field=field, website_value=jsonable(external), agentready_value=jsonable(current),
                        original_value=jsonable((p.source_values or {}).get(field)), evidence=[{
                            "source_type": f"{i.provider}_api", "source_url": i.store_url, "value": jsonable(external)}],
                        suggested_action="keep_agentready",
                        suggestion_reason=f"You changed this in AgentReady; {i.provider.title()} has a different value."))


async def _open_conflict_exists(db, product_id, field) -> bool:
    return (await db.execute(select(SyncConflict.id).where(SyncConflict.product_id == product_id, SyncConflict.field == field,
                                                           SyncConflict.status == "open"))).first() is not None


async def _set_stock(db, p: Product, variant: ProductVariant | None, quantity: int, actor: Actor, job: SyncJob) -> None:
    row = variant or p
    if not row.track_inventory:
        row.track_inventory = True
        await db.flush()
    if row.stock_on_hand != quantity:
        await change_stock(db, p, actor=actor, variant=variant, operation="set", quantity=max(quantity, 0) if row.backorder_policy != "allow" else quantity,
                           reason="Imported from store", transaction_type="integration_import",
                           related_operation=f"sync:{job.id}")


async def apply_external(db: AsyncSession, i: CommerceIntegration, job: SyncJob, ext: ExternalProduct, actor: Actor,
                         business: Business) -> str:
    """Create or update one product from the store. Returns created|updated|unchanged|conflict."""
    link = (await db.execute(select(IntegrationLink).where(
        IntegrationLink.integration_id == i.id, IntegrationLink.external_id == ext.external_id,
        IntegrationLink.external_variant_id == ""))).scalar_one_or_none()
    fingerprint = ext.fingerprint()
    product = await db.get(Product, link.product_id) if link else None
    if link and link.last_pulled_hash == fingerprint and product is not None:
        return "unchanged"
    inventory_master = (i.settings or {}).get("inventory_master", "store")
    created = False
    if product is None and ext.sku:  # link to an existing product with the same SKU instead of duplicating it
        product = (await db.execute(select(Product).where(
            Product.business_id == i.business_id, Product.deleted_at.is_(None),
            func.lower(Product.sku) == ext.sku.lower()))).scalars().first()
    if product is None:
        created = True
        product = Product(
            business_id=i.business_id, match_key=f"{i.provider}:{i.id}:{ext.external_id}", origin=i.provider,
            name=ext.name, sku=ext.sku, description=ext.description, brand=ext.brand, product_type=ext.product_type,
            lifecycle_status=ext.status if ext.status in ("active", "draft", "archived") else "draft",
            approval_status="needs_review", publication_status="unpublished", images=ext.images[:20],
            product_url=ext.url, source_url=ext.url, gtin=ext.gtin if ext.gtin and re.fullmatch(r"\d{8,14}", ext.gtin) else None,
            availability=ext.availability, attributes=[], variants=[], extra={}, manual_overrides=[], shipping={},
        )
        product.currency = i.currency or business.default_currency  # store currency, also for variant-only prices
        cat = await _category_for(db, i.business_id, ext.categories[0] if ext.categories else None)
        product.category_id = cat.id if cat else None
        product.category = cat.path if cat else (ext.categories[0][:300] if ext.categories else None)
        product.attributes = [ProductAttribute(key=attribute_key(k), label=k[:200], value=v, raw_value=v, source="extracted",
                                               position=n) for n, (k, v) in enumerate(ext.attributes[:100])]
        db.add(product)
        await db.flush()
        for f in ("name", "sku", "description", "brand"):
            if getattr(product, f):
                db.add(ProductEvidence(product_id=product.id, business_id=product.business_id, field=f,
                                       value=jsonable(getattr(product, f))[:2000] if isinstance(getattr(product, f), str) else None,
                                       source_value=f"{i.provider.title()} API", source_url=ext.url,
                                       source_type=f"{i.provider}_api", extraction_method="integration"))
        record(db, i.business_id, actor, "product.created", "product", product.id,
               f"'{product.name}' imported from {i.provider.title()}", {"external_id": ext.external_id}, job.id)
    overrides = set(product.manual_overrides or [])
    outcome = "created" if created else "unchanged"
    conflicts = 0
    # ---- content fields: update unless merchant-managed (then conflict)
    if not created:
        for f in ("name", "description", "brand", "sku"):
            ext_v = getattr(ext, f)
            if ext_v and ext_v != getattr(product, f):
                if f in overrides:
                    if not await _open_conflict_exists(db, product.id, f):
                        _conflict(db, i, job, product, f, ext_v, getattr(product, f))
                        conflicts += 1
                else:
                    setattr(product, f, ext_v)
                    outcome = "updated"
    # ---- price
    if ext.regular_price is not None and (ext.regular_price != product.price_amount or ext.sale_price != product.sale_price_amount):
        if "price" in overrides and not created:
            if not await _open_conflict_exists(db, product.id, "price_amount"):
                _conflict(db, i, job, product, "price_amount", fmt(ext.regular_price), fmt(product.price_amount))
                conflicts += 1
        else:
            await set_pricing(db, product, actor=actor, change_type="integration_import", regular=ext.regular_price,
                              sale=ext.sale_price if ext.sale_price and ext.sale_price < ext.regular_price else None,
                              currency=product.currency or i.currency or business.default_currency,
                              reason=f"Imported from {i.provider.title()}", mark_override=False, audit=not created)
            outcome = "updated" if not created else outcome
    # ---- stock
    if ext.manage_stock and ext.stock_quantity is not None and inventory_master == "store" and not ext.variants:
        if not product.track_inventory or product.stock_on_hand != ext.stock_quantity:
            await _set_stock(db, product, None, ext.stock_quantity, actor, job)
            outcome = "updated" if not created else outcome
    # ---- variants
    for ev in ext.variants:
        outcome = await _apply_variant(db, i, job, product, ev, actor, inventory_master, created) or outcome
    if link is None:
        link = IntegrationLink(business_id=i.business_id, integration_id=i.id, product_id=product.id,
                               external_id=ext.external_id, external_variant_id="")
        db.add(link)
    link.last_pulled_hash, link.last_synced_at = fingerprint, now_utc()
    if ext.default_variant_id:
        sv = dict(product.extra or {})
        sv[f"{i.provider}_variant"] = {"id": ext.default_variant_id, "inventory_item_id": ext.inventory_item_id}
        product.extra = sv
    product.source_values = {**(product.source_values or {}), "price_amount": fmt(ext.regular_price), "name": ext.name}
    if outcome != "unchanged" and not created:
        product.version += 1
        product.updated_at = now_utc()
        if product.approval_status == "approved":
            from app.services.commerce.lifecycle import set_approval

            set_approval(db, product, "needs_review", actor, job.id)  # store data changed: re-approval required
    job.conflicts_detected += conflicts
    return "conflict" if conflicts and outcome == "unchanged" else outcome


async def _apply_variant(db, i, job, product: Product, ev: ExternalVariant, actor, inventory_master, created) -> str | None:
    link = (await db.execute(select(IntegrationLink).where(
        IntegrationLink.integration_id == i.id, IntegrationLink.external_variant_id == ev.external_id,
        IntegrationLink.product_id == product.id))).scalar_one_or_none()
    variant = await db.get(ProductVariant, link.variant_id) if link and link.variant_id else None
    changed = None
    if variant is None:
        variant = ProductVariant(product_id=product.id, business_id=product.business_id, name=ev.name, sku=ev.sku,
                                 attributes=ev.options, combination_key=combination_key(ev.options), origin="integration",
                                 image_url=ev.image_url, position=len(product.variants))
        product.variants.append(variant)
        await db.flush()
        db.add(IntegrationLink(business_id=i.business_id, integration_id=i.id, product_id=product.id, variant_id=variant.id,
                               external_id=(await _product_external_id(db, i, product)) or "", external_variant_id=ev.external_id))
        changed = "updated" if not created else None
    if ev.regular_price is not None and (ev.regular_price != variant.price_amount or ev.sale_price != variant.sale_price_amount):
        await set_pricing(db, product, actor=actor, change_type="integration_import", variant=variant, regular=ev.regular_price,
                          sale=ev.sale_price if ev.sale_price and ev.sale_price < ev.regular_price else None,
                          mark_override=False, audit=False)
        changed = "updated" if not created else changed
    if ev.manage_stock and ev.stock_quantity is not None and inventory_master == "store":
        if not variant.track_inventory or variant.stock_on_hand != ev.stock_quantity:
            await _set_stock(db, product, variant, ev.stock_quantity, actor, job)
            changed = "updated" if not created else changed
    return changed


async def _product_external_id(db, i, product) -> str | None:
    return (await db.execute(select(IntegrationLink.external_id).where(
        IntegrationLink.integration_id == i.id, IntegrationLink.product_id == product.id,
        IntegrationLink.external_variant_id == ""))).scalar_one_or_none() or (
        product.match_key.split(":")[-1] if product.match_key.startswith(f"{i.provider}:") else None)


async def run_import(db: AsyncSession, i: CommerceIntegration, actor: Actor, trigger: str = "manual", client=None,
                     job: SyncJob | None = None) -> SyncJob:
    from app.services.validation.validator import revalidate_business

    business = await db.get(Business, i.business_id)
    if job is None:
        job = SyncJob(business_id=i.business_id, source_type=i.provider, integration_id=i.id, source_label=i.store_url,
                      direction="import", trigger=trigger, status="running", created_by=actor.id, errors=[], summary={})
        db.add(job)
    job.status, job.started_at = "running", now_utc()
    await db.commit()
    counts = {"created": 0, "updated": 0, "unchanged": 0, "conflict": 0}
    errors: list[dict] = []
    provider = None
    try:
        provider = provider_for(i, client)
        async for ext in provider.iter_products():
            job.products_discovered += 1
            try:
                async with db.begin_nested():
                    outcome = await apply_external(db, i, job, ext, actor, business)
                counts[outcome] = counts.get(outcome, 0) + 1
            except (InventoryError, ValueError, IntegrityError) as e:
                errors.append({"external_id": ext.external_id, "name": ext.name, "message": str(e)[:300]})
            if job.products_discovered % 25 == 0:
                await db.commit()
        job.status = "completed_with_errors" if errors else "completed"
        i.status, i.last_error = "connected", None
    except SYNC_ERRORS as e:
        job.status = "failed"
        errors.append({"message": str(e)})
        i.status, i.last_error = "error", str(e)
    finally:
        if provider is not None:
            await provider.aclose()
    job.products_created, job.products_changed = counts["created"], counts["updated"]
    job.products_ignored = counts["unchanged"]
    job.failed_operations = len(errors)
    job.errors, job.summary, job.finished_at = errors[:100], counts, now_utc()
    i.last_sync_at = now_utc()
    await db.flush()
    await revalidate_business(db, i.business_id)
    record(db, i.business_id, actor, "sync.completed", "integration", i.id,
           f"{i.provider.title()} import {job.status.replace('_', ' ')}: {counts['created']} created, "
           f"{counts['updated']} updated, {job.conflicts_detected} conflicts, {len(errors)} failed",
           {"sync_job_id": job.id, **counts}, job.id)
    await db.commit()
    return job


# ---------------------------------------------------------------- push (AgentReady -> store)
async def run_push(db: AsyncSession, i: CommerceIntegration, actor: Actor, product_ids: list[uuid.UUID] | None = None,
                   push_prices: bool = True, push_inventory: bool = True, client=None) -> SyncJob:
    job = SyncJob(business_id=i.business_id, source_type=i.provider, integration_id=i.id, source_label=i.store_url,
                  direction="export", trigger="manual", status="running", started_at=now_utc(), created_by=actor.id,
                  errors=[], summary={})
    db.add(job)
    await db.commit()
    q = select(IntegrationLink).where(IntegrationLink.integration_id == i.id, IntegrationLink.business_id == i.business_id)
    if product_ids:
        q = q.where(IntegrationLink.product_id.in_(product_ids))
    links = (await db.execute(q)).scalars().all()
    errors, pushed = [], 0
    provider = None
    try:
        provider = provider_for(i, client)
        for link in links:
            p = await db.get(Product, link.product_id)
            v = await db.get(ProductVariant, link.variant_id) if link.variant_id else None
            if p is None or p.deleted_at is not None:
                continue
            row = v or p
            job.products_discovered += 1
            ext_variant = link.external_variant_id or ((p.extra or {}).get(f"{i.provider}_variant") or {}).get("id")
            inv_item = ((p.extra or {}).get(f"{i.provider}_variant") or {}).get("inventory_item_id")
            try:
                regular = row.price_amount if v is None or v.price_amount is not None else p.price_amount
                if push_prices and regular is not None:
                    await provider.update_price(link.external_id, ext_variant, regular, row.sale_price_amount)
                if push_inventory and row.track_inventory:
                    await provider.update_inventory(link.external_id, ext_variant, row.stock_on_hand - row.stock_reserved, inv_item)
                link.last_pushed_hash = hashlib.sha256(json.dumps(
                    [fmt(regular), fmt(row.sale_price_amount), row.stock_on_hand]).encode()).hexdigest()
                link.last_synced_at = now_utc()
                pushed += 1
            except SYNC_ERRORS as e:
                errors.append({"product_id": str(p.id), "name": p.name, "message": str(e)[:300]})
        job.status = "failed" if errors and not pushed else ("completed_with_errors" if errors else "completed")
    except SYNC_ERRORS as e:
        job.status = "failed"
        errors.append({"message": str(e)})
    finally:
        if provider is not None:
            await provider.aclose()
    job.products_changed, job.failed_operations = pushed, len(errors)
    job.errors, job.summary, job.finished_at = errors[:100], {"pushed": pushed}, now_utc()
    record(db, i.business_id, actor, "sync.pushed", "integration", i.id,
           f"Pushed {pushed} item(s) to {i.provider.title()} ({len(errors)} failed)", {"sync_job_id": job.id}, job.id)
    await db.commit()
    return job


# ---------------------------------------------------------------- webhooks
def _woo_payload(body: dict) -> ExternalProduct | None:
    if not isinstance(body, dict) or "id" not in body:
        return None
    return ExternalProduct(
        external_id=str(body["id"]), name=body.get("name"), sku=body.get("sku") or None,
        description=re.sub(r"<[^>]+>", " ", body.get("description") or "").strip() or None,
        status={"publish": "active"}.get(body.get("status"), "draft"),
        regular_price=_money(body.get("regular_price")), sale_price=_money(body.get("sale_price")),
        manage_stock=body.get("manage_stock") is True, stock_quantity=body.get("stock_quantity"),
        images=[{"url": x["src"], "alt": x.get("alt")} for x in body.get("images") or [] if x.get("src")],
        url=body.get("permalink"),
    )


def _shopify_payload(body: dict) -> ExternalProduct | None:
    if not isinstance(body, dict) or "id" not in body:
        return None
    variants = body.get("variants") or []
    single = len(variants) == 1
    base = variants[0] if single else {}
    price, compare = _money(base.get("price")), _money(base.get("compare_at_price"))
    regular, sale = (compare, price) if compare and price and compare > price else (price, None)
    return ExternalProduct(
        external_id=body.get("admin_graphql_api_id") or f"gid://shopify/Product/{body['id']}", name=body.get("title"),
        sku=base.get("sku") or None, brand=body.get("vendor") or None,
        regular_price=regular if single else None, sale_price=sale if single else None,
        status={"active": "active", "archived": "archived"}.get(body.get("status"), "draft"))


async def handle_webhook(db: AsyncSession, i: CommerceIntegration, headers: dict[str, str], raw: bytes) -> tuple[int, str]:
    cls = PROVIDERS[i.provider]
    if i.provider == "shopify":
        from app.core.config import get_settings

        secret = get_settings().shopify_api_secret
    else:
        secret = decrypt_json(i.webhook_secret_encrypted).get("secret", "") if i.webhook_secret_encrypted else ""
    if not cls.verify_webhook(secret, headers, raw):
        return 401, "invalid signature"
    delivery, topic = cls.webhook_meta(headers)
    delivery = delivery or hashlib.sha256(raw).hexdigest()
    event = IntegrationWebhookEvent(business_id=i.business_id, integration_id=i.id, delivery_id=delivery[:200],
                                    topic=(topic or "")[:120])
    db.add(event)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return 200, "duplicate delivery ignored"
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        event.status, event.detail = "failed", "body is not JSON"
        await db.commit()
        return 400, "invalid body"
    if not topic or not topic.replace("/", ".").startswith(("product", "products")):
        event.status, event.detail = "ignored", f"topic {topic} not handled"
        await db.commit()
        return 200, "ignored"
    ext = _woo_payload(body) if i.provider == "woocommerce" else _shopify_payload(body)
    if ext is None:
        event.status, event.detail = "ignored", "no product in payload"
        await db.commit()
        return 200, "ignored"
    job = SyncJob(business_id=i.business_id, source_type=i.provider, integration_id=i.id, source_label=i.store_url,
                  direction="import", trigger="webhook", status="running", started_at=now_utc(), errors=[], summary={},
                  products_discovered=1)
    db.add(job)
    await db.flush()
    business = await db.get(Business, i.business_id)
    try:
        async with db.begin_nested():
            outcome = await apply_external(db, i, job, ext, Actor("integration", str(i.id), f"{i.provider.title()} webhook"),
                                           business)
        job.status, job.summary = "completed", {"outcome": outcome}
        job.products_created = int(outcome == "created")
        job.products_changed = int(outcome == "updated")
        job.products_ignored = int(outcome == "unchanged")
        event.status, event.detail = "processed", outcome
    except (InventoryError, ValueError) as e:
        job.status, job.failed_operations, job.errors = "failed", 1, [{"message": str(e)[:300]}]
        event.status, event.detail = "failed", str(e)[:500]
    job.finished_at = now_utc()
    await db.commit()
    return 200, event.detail or "ok"
