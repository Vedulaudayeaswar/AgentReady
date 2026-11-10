"""Commerce actions foundation: validated carts and auditable checkout hand-offs.

AgentReady does not take payment, store card data or create orders. A checkout session verifies every item
against live data (publication, distribution permissions, current price and currency, availability, quantity)
and then hands the shopper to the merchant's own checkout:

* ``shopify_cart_permalink``   — every item is linked to a Shopify variant through a connected integration:
  ``https://{shop}/cart/{variant_id}:{qty},...`` (Shopify cart permalinks).
* ``woocommerce_add_to_cart``  — a single item linked to a WooCommerce product: ``{store}/?add-to-cart={id}&quantity={n}``.
* ``merchant_product_urls``    — otherwise, each item's verified product page on the merchant's website.

``payment_status`` is always ``not_handled`` and ``order_status`` is ``unknown`` because no order provider reports
back to AgentReady; nothing is ever reported as paid.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Cart, CheckoutSession, CommerceIntegration, IntegrationLink, Product, ProductVariant
from app.services.commerce.audit import Actor, record
from app.services.commerce.money import aware, fmt, now_utc
from app.services.distribution.agent import AgentContext, AgentError, ensure_fresh_prices, scopes_for
from app.services.distribution.public import eligibility_clause, live_variants, variant_availability, variant_price
from app.services.distribution.public import availability_of, product_price

CART_TTL = timedelta(minutes=30)
MAX_QTY = 100
PURCHASABLE = ("available", "backorder", "preorder")


async def _scope(db, actx: AgentContext):
    if actx.mode != "api_key" or "checkout:write" not in actx.scopes:
        raise AgentError(403, "forbidden", "Carts and checkout need an API key with the 'checkout:write' scope")
    scopes = await scopes_for(db, actx)
    sc = scopes[0]
    if not sc.profile.checkout_enabled:
        raise AgentError(403, "checkout_disabled", "This merchant has not enabled checkout hand-offs")
    await ensure_fresh_prices(db, scopes)
    return sc


async def validate_items(db, actx: AgentContext, sc, items: list[dict]) -> tuple[list[dict], list[dict], str | None, Decimal]:
    """Returns (verified lines, issues, currency, subtotal). Never trusts client-supplied prices."""
    lines, issues = [], []
    currency = None
    subtotal = Decimal(0)
    at = now_utc()
    for idx, it in enumerate(items):
        ref = {"index": idx, "product_id": it.get("product_id"), "variant_id": it.get("variant_id")}
        qty = it.get("quantity", 1)
        if not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            issues.append({**ref, "code": "invalid_quantity", "message": f"Quantity must be 1-{MAX_QTY}"})
            continue
        try:
            pid = uuid.UUID(str(it.get("product_id")))
        except ValueError:
            issues.append({**ref, "code": "not_found", "message": "Product not found"})
            continue
        p = (await db.execute(select(Product).where(eligibility_clause(sc, actx.channel), Product.id == pid))).scalar_one_or_none()
        if p is None:
            issues.append({**ref, "code": "not_found", "message": "Product not found or not distributed"})
            continue
        variants = live_variants(p)
        v = None
        if it.get("variant_id"):
            v = next((x for x in variants if str(x.id) == str(it["variant_id"])), None)
            if v is None:
                issues.append({**ref, "code": "variant_not_found", "message": "Variant not found or not available"})
                continue
        elif variants:
            issues.append({**ref, "code": "variant_required", "message": "Choose a variant",
                           "variants": [{"id": str(x.id), "options": x.attributes or {}} for x in variants]})
            continue
        price = variant_price(v, p, at) if v else product_price(p, at)
        avail = (variant_availability(v, p) if v else availability_of(p))["status"]
        if price is None:
            issues.append({**ref, "code": "no_price", "message": "This item has no public price"})
            continue
        if avail not in PURCHASABLE:
            issues.append({**ref, "code": "unavailable", "message": "Availability is unknown, so it cannot be verified"
                           if avail == "unknown" else f"Item is {avail.replace('_', ' ')}"})
            continue
        row = v if v is not None and v.track_inventory else (p if p.track_inventory else None)
        if row is not None and row.backorder_policy != "allow" and row.stock_on_hand - row.stock_reserved < qty:
            # the available quantity itself is private: only say that the quantity cannot be fulfilled
            issues.append({**ref, "code": "insufficient_stock", "message": "The requested quantity is not available"})
            continue
        if currency and price["currency"] != currency:
            issues.append({**ref, "code": "currency_mismatch", "message": "All items in a cart must use one currency"})
            continue
        currency = price["currency"]
        unit = Decimal(price["current"])
        if it.get("expected_unit_price") is not None and Decimal(str(it["expected_unit_price"])) != unit:
            issues.append({**ref, "code": "price_changed", "message": "The price changed", "current_unit_price": fmt(unit)})
        line_total = unit * qty
        subtotal += line_total
        lines.append({"product_id": str(p.id), "variant_id": str(v.id) if v else None, "name": p.name,
                      "variant_name": v.name if v else None, "sku": (v.sku if v else None) or p.sku, "quantity": qty,
                      "unit_price": fmt(unit), "line_total": fmt(line_total), "currency": currency,
                      "availability": avail, "url": p.product_url or p.source_url})
    return lines, issues, currency, subtotal


def cart_dict(c: Cart) -> dict:
    return {"id": str(c.id), "status": c.status if aware(c.expires_at) > now_utc() or c.status != "open" else "expired",
            "currency": c.currency, "items": c.items, "subtotal": fmt(c.subtotal), "issues": c.issues,
            "expires_at": aware(c.expires_at).isoformat(), "created_at": aware(c.created_at).isoformat() if c.created_at else None,
            "note": "Prices are verified by AgentReady from live merchant data; taxes and shipping are calculated by the merchant at checkout."}


async def create_cart(db: AsyncSession, actx: AgentContext, items: list[dict]) -> dict:
    if not 1 <= len(items) <= 50:
        raise AgentError(422, "validation_error", "A cart needs 1-50 items")
    sc = await _scope(db, actx)
    lines, issues, currency, subtotal = await validate_items(db, actx, sc, items)
    cart = Cart(business_id=sc.business_id, api_key_id=uuid.UUID(actx.api_key_id), status="open", currency=currency,
                items=lines, subtotal=subtotal if lines else None, issues=issues, expires_at=now_utc() + CART_TTL)
    db.add(cart)
    await db.flush()
    from app.services.distribution import analytics

    analytics.record(db, sc.business_id, "cart", "checkout", actx.consumer, result_count=len(lines),
                     meta={"issues": len(issues)})
    await db.commit()
    return cart_dict(cart)


async def _owned_cart(db, actx: AgentContext, cart_id: str) -> Cart:
    try:
        cid = uuid.UUID(cart_id)
    except ValueError:
        raise AgentError(404, "not_found", "Cart not found")
    cart = await db.get(Cart, cid)
    if cart is None or actx.api_key_id is None or str(cart.api_key_id) != actx.api_key_id:
        raise AgentError(404, "not_found", "Cart not found")
    return cart


async def get_cart(db, actx, cart_id: str) -> dict:
    return cart_dict(await _owned_cart(db, actx, cart_id))


async def _checkout_target(db, sc, lines: list[dict]) -> tuple[str, str | None, list[dict]]:
    pids = [uuid.UUID(line["product_id"]) for line in lines]
    links = (await db.execute(select(IntegrationLink, CommerceIntegration).join(
        CommerceIntegration, CommerceIntegration.id == IntegrationLink.integration_id).where(
        IntegrationLink.business_id == sc.business_id, IntegrationLink.product_id.in_(pids),
        CommerceIntegration.status == "connected"))).all()
    shop_parts, shop_host = [], None
    for line in lines:
        match = next((ln for ln, i in links if i.provider == "shopify" and str(ln.product_id) == line["product_id"]
                      and ((line["variant_id"] and str(ln.variant_id) == line["variant_id"]) or
                           (not line["variant_id"] and ln.external_variant_id))), None)
        integ = next((i for ln, i in links if match is not None and ln.id == match.id), None)
        if match is None or not match.external_variant_id:
            shop_parts = []
            break
        host = urlsplit(integ.store_url).hostname
        if shop_host and host != shop_host:
            shop_parts = []
            break
        shop_host = host
        shop_parts.append(f"{match.external_variant_id.rsplit('/', 1)[-1]}:{line['quantity']}")
    if shop_parts and shop_host:
        return "shopify_cart_permalink", f"https://{shop_host}/cart/{','.join(shop_parts)}", []
    if len(lines) == 1:
        woo = next(((ln, i) for ln, i in links if i.provider == "woocommerce" and str(ln.product_id) == lines[0]["product_id"]), None)
        if woo is not None:
            ln, integ = woo
            ext = ln.external_variant_id or ln.external_id
            base = integ.store_url.rstrip("/")
            return "woocommerce_add_to_cart", f"{base}/?{urlencode({'add-to-cart': ext, 'quantity': lines[0]['quantity']})}", []
    urls = []
    site_hosts = {urlsplit(sc.profile.public_url).hostname} if sc.profile.public_url else set()
    for line in lines:
        u = line.get("url")
        host = urlsplit(u).hostname if u else None
        # a product URL is "verified" when it is on the merchant's declared website or came from their own crawl/import
        urls.append({"product_id": line["product_id"], "variant_id": line["variant_id"], "url": u,
                     "verified": bool(u) and (not site_hosts or host in site_hosts)})
    return "merchant_product_urls", None, urls


async def create_checkout(db: AsyncSession, actx: AgentContext, cart_id: str, expected_subtotal: str | None,
                          expected_currency: str | None) -> dict:
    cart = await _owned_cart(db, actx, cart_id)
    if aware(cart.expires_at) <= now_utc():
        raise AgentError(410, "cart_expired", "This cart has expired; create a new one")
    sc = await _scope(db, actx)
    items = [{"product_id": line["product_id"], "variant_id": line["variant_id"], "quantity": line["quantity"]}
             for line in cart.items]
    lines, issues, currency, subtotal = await validate_items(db, actx, sc, items)
    if not cart.items:
        raise AgentError(422, "empty_cart", "The cart has no valid items")
    if issues:
        raise AgentError(409, "validation_failed", "Some items can no longer be purchased as requested", {"issues": issues})
    if expected_currency and expected_currency != currency:
        raise AgentError(409, "currency_changed", "The cart currency differs from the expected currency",
                         {"currency": currency})
    if expected_subtotal is not None and Decimal(str(expected_subtotal)) != subtotal:
        raise AgentError(409, "price_changed", "Prices changed since the cart was created",
                         {"current_subtotal": fmt(subtotal), "items": lines})
    method, url, item_urls = await _checkout_target(db, sc, lines)
    sess = CheckoutSession(business_id=sc.business_id, cart_id=cart.id, api_key_id=cart.api_key_id, status="ready",
                           method=method, checkout_url=url, item_urls=item_urls, currency=currency, items=lines,
                           subtotal=subtotal, payment_status="not_handled", order_status="unknown",
                           expires_at=now_utc() + CART_TTL)
    db.add(sess)
    cart.status, cart.items, cart.subtotal = "checkout_created", lines, subtotal
    await db.flush()
    from app.services.distribution import analytics

    analytics.record(db, sc.business_id, "checkout_session", "checkout", actx.consumer, result_count=len(lines),
                     meta={"method": method})
    record(db, sc.business_id, Actor("api_key", actx.api_key_id, actx.label or "API key"), "checkout.session_created",
           "checkout_session", sess.id, f"Checkout hand-off created ({method}, {len(lines)} items, {fmt(subtotal)} {currency})",
           {"method": method, "items": len(lines), "subtotal": fmt(subtotal), "currency": currency})
    await db.commit()
    return session_dict(sess)


def session_dict(s: CheckoutSession) -> dict:
    expired = aware(s.expires_at) <= now_utc()
    return {"id": str(s.id), "cart_id": str(s.cart_id), "status": "expired" if expired and s.status == "ready" else s.status,
            "method": s.method, "checkout_url": s.checkout_url, "item_urls": s.item_urls, "currency": s.currency,
            "items": s.items, "subtotal": fmt(s.subtotal), "payment_status": s.payment_status, "order_status": s.order_status,
            "expires_at": aware(s.expires_at).isoformat(), "created_at": aware(s.created_at).isoformat() if s.created_at else None,
            "notes": ["AgentReady does not process payments or store card data.",
                      "The shopper completes the purchase on the merchant's own checkout.",
                      "Order and payment status are unknown to AgentReady unless an order provider reports them."]}


async def get_session(db, actx, session_id: str) -> dict:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise AgentError(404, "not_found", "Checkout session not found")
    s = await db.get(CheckoutSession, sid)
    if s is None or actx.api_key_id is None or str(s.api_key_id) != actx.api_key_id:
        raise AgentError(404, "not_found", "Checkout session not found")
    return session_dict(s)


__all__ = ["create_cart", "get_cart", "create_checkout", "get_session", "ProductVariant"]
