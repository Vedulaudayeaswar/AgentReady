"""Deterministic anomaly detection and price/inventory insights.

Rules only (no ML). Each anomaly records what was detected, the product, why it is suspicious, the evidence, when it
was detected and what to investigate. Detection NEVER changes merchant-managed values. Anomalies that no longer
apply are marked resolved automatically; dismissed ones stay dismissed while their evidence is unchanged.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (CommerceIntegration, CrawledPage, InventoryTransaction, PriceHistory, Product, ProductAnomaly,
                        ScheduledPriceChange, SyncConflict, SyncJob, live_products)
from app.services.commerce.money import aware, fmt, now_utc
from app.services.distribution.public import live_variants
from app.services.distribution.units import UNIT_DIMENSION, canonical_unit
from app.services.intelligence.engine import get_settings

RULES = {
    "stale_product": "Product not updated for a long period",
    "sudden_price_change": "Sudden price change",
    "unusual_price": "Price far from the rest of its category",
    "missing_currency": "Price without currency",
    "availability_conflict": "Availability conflicts with tracked stock",
    "duplicate_identifier": "Identifier used by several products",
    "inconsistent_variants": "Variants use different option names",
    "invalid_unit": "Attribute unit not recognised",
    "website_conflict": "Website value conflicts with the managed value",
    "page_gone": "Product page no longer found",
    "broken_image": "Product image not reachable",
    "sync_failed": "Synchronization failed",
    "repeated_adjustments": "Repeated inventory adjustments",
}


def _a(fp, rule, severity, product, title, detail, evidence, suggestion):
    return {"fingerprint": fp, "rule": rule, "severity": severity, "product_id": product.id if product is not None else None,
            "title": title, "detail": detail, "evidence": evidence, "suggestion": suggestion}


async def detect(db: AsyncSession, business_id: uuid.UUID) -> list[dict]:
    s = await get_settings(db, business_id)
    now = now_utc()
    products = list((await db.execute(select(Product).where(*live_products(business_id)))).scalars())
    out: list[dict] = []
    for p in products:
        age = (now - aware(p.updated_at)).days if p.updated_at else 0
        if age > s.stale_after_days:
            out.append(_a(f"stale:{p.id}", "stale_product", "low", p, f"'{p.name}' not updated for {age} days",
                          f"No change recorded since {aware(p.updated_at).date()}.", {"updated_at": aware(p.updated_at).isoformat(),
                                                                                     "threshold_days": s.stale_after_days},
                          "Confirm the product details are still current."))
        if p.price_amount is not None and not p.currency:
            out.append(_a(f"cur:{p.id}", "missing_currency", "high", p, f"'{p.name}' has a price but no currency",
                          "Agents and feeds cannot interpret a price without its currency.", {"price": fmt(p.price_amount)},
                          "Set the currency on the product's pricing tab."))
        if p.track_inventory and p.stock_status in ("out_of_stock",) and (p.availability or "").lower() == "instock":
            out.append(_a(f"avail:{p.id}", "availability_conflict", "medium", p, f"'{p.name}' is out of stock but marked in stock",
                          "Tracked stock says out of stock while the availability text says InStock.",
                          {"stock_status": p.stock_status, "availability": p.availability}, "Review the stock count and availability."))
        vs = live_variants(p)
        keysets = {tuple(sorted((v.attributes or {}).keys())) for v in vs}
        if len(keysets) > 1:
            out.append(_a(f"var:{p.id}", "inconsistent_variants", "medium", p, f"'{p.name}' variants use different option names",
                          "Some variants are described with different options, which breaks option filters (e.g. size:M).",
                          {"option_sets": [list(k) for k in keysets]}, "Use the same option names for every variant."))
        bad_units = [f"{a.label}: {a.unit}" for a in p.attributes if a.unit and canonical_unit(a.unit) is None and a.unit not in UNIT_DIMENSION
                     and a.unit not in ("%", "pcs", "Ω", "W/m", "kg·cm")]
        if bad_units:
            out.append(_a(f"unit:{p.id}", "invalid_unit", "low", p, f"'{p.name}' has attribute units that cannot be normalised",
                          "Values with unrecognised units cannot be compared or filtered by agents.", {"attributes": bad_units},
                          "Use a standard unit (e.g. V, W, kg, mm, GB)."))
        if p.is_removed:
            out.append(_a(f"gone:{p.id}", "page_gone", "medium", p, f"'{p.name}' was not found on the website",
                          "The last rescan did not find this product's page.", {"removed_at": aware(p.removed_at).isoformat() if p.removed_at else None},
                          "Check whether the product was discontinued or its page moved."))

    # duplicate identifiers
    for field in ("gtin", "sku"):
        groups: dict[str, list[Product]] = defaultdict(list)
        for p in products:
            v = getattr(p, field)
            if v:
                groups[v.strip().lower()].append(p)
        for v, ps in groups.items():
            if len(ps) > 1:
                out.append(_a(f"dup:{field}:{v}", "duplicate_identifier", "high", ps[0],
                              f"{field.upper()} {v} is used by {len(ps)} products",
                              "Duplicate identifiers make products ambiguous for agents and marketplaces.",
                              {"field": field, "value": v, "products": [{"id": str(x.id), "name": x.name} for x in ps]},
                              "Check whether these are duplicates or one identifier is wrong."))

    # unusual prices: > 10x or < 1/10 of the category median, same currency, categories with >= 5 priced products
    by_cat: dict[tuple, list[Product]] = defaultdict(list)
    for p in products:
        if p.price_amount is not None and p.currency:
            by_cat[(p.category or "", p.currency)].append(p)
    for (cat, cur), ps in by_cat.items():
        if len(ps) < 5:
            continue
        prices = sorted(Decimal(p.price_amount) for p in ps)
        median = prices[len(prices) // 2]
        for p in ps:
            if median > 0 and (p.price_amount > median * 10 or p.price_amount * 10 < median):
                out.append(_a(f"price_outlier:{p.id}", "unusual_price", "medium", p, f"'{p.name}' is priced far from its category",
                              f"{fmt(p.price_amount)} {cur} vs a median of {fmt(median)} {cur} across {len(ps)} products in "
                              f"'{cat or 'uncategorised'}'.", {"price": fmt(p.price_amount), "median": fmt(median), "currency": cur,
                                                               "products_compared": len(ps)},
                              "Check for a missing or extra digit, or a wrong currency."))

    # sudden price changes (latest regular-price change in the last 30 days)
    since = now - timedelta(days=30)
    hist = (await db.execute(select(PriceHistory).where(PriceHistory.business_id == business_id, PriceHistory.price_type == "regular",
                                                        PriceHistory.created_at >= since, PriceHistory.old_amount.is_not(None),
                                                        PriceHistory.new_amount.is_not(None))
                             .order_by(PriceHistory.created_at))).scalars().all()
    names = {p.id: p for p in products}
    for h in hist:
        if h.old_amount and h.old_amount > 0:
            pct = float((h.new_amount - h.old_amount) / h.old_amount * 100)
            if abs(pct) >= s.price_change_alert_percent and h.product_id in names:
                p = names[h.product_id]
                out.append(_a(f"jump:{h.id}", "sudden_price_change", "high" if abs(pct) >= 75 else "medium", p,
                              f"'{p.name}' price changed {pct:+.0f}%",
                              f"{fmt(h.old_amount)} → {fmt(h.new_amount)} {h.currency or ''} ({h.change_type}).",
                              {"old": fmt(h.old_amount), "new": fmt(h.new_amount), "currency": h.currency, "percent": round(pct, 1),
                               "change_type": h.change_type, "at": aware(h.created_at).isoformat(), "actor": h.actor_label},
                              "Confirm the new price is intended."))

    # open website conflicts
    for c in (await db.execute(select(SyncConflict).where(SyncConflict.business_id == business_id, SyncConflict.status == "open"))).scalars():
        p = names.get(c.product_id)
        if p is not None:
            out.append(_a(f"conflict:{c.id}", "website_conflict", "medium", p, f"Website changed '{c.field}' of '{p.name}'",
                          "The website shows a different value than the one you manage.",
                          {"field": c.field, "website": c.website_value, "agentready": c.agentready_value,
                           "previous_website": c.original_value}, "Resolve it in Synchronization."))

    # failed synchronizations (last 7 days) and integrations in error
    for j in (await db.execute(select(SyncJob).where(SyncJob.business_id == business_id, SyncJob.status == "failed",
                                                     SyncJob.created_at >= now - timedelta(days=7)))).scalars():
        out.append(_a(f"sync:{j.id}", "sync_failed", "high", None, f"Synchronization from {j.source_label or j.source_type} failed",
                      "; ".join(str(e) for e in (j.errors or [])[:2]) or "The sync job ended with status failed.",
                      {"sync_job_id": str(j.id), "at": aware(j.created_at).isoformat()}, "Open Synchronization to retry."))
    for i in (await db.execute(select(CommerceIntegration).where(CommerceIntegration.business_id == business_id,
                                                                 CommerceIntegration.status == "error"))).scalars():
        out.append(_a(f"integration:{i.id}", "sync_failed", "high", None, f"{i.provider.title()} integration has an error",
                      i.last_error or "The store connection reported an error.", {"integration_id": str(i.id)},
                      "Reconnect or re-verify the store."))

    # repeated manual adjustments: >= 5 in 7 days
    rep = (await db.execute(select(InventoryTransaction.product_id, func.count()).where(
        InventoryTransaction.business_id == business_id, InventoryTransaction.created_at >= now - timedelta(days=7))
        .group_by(InventoryTransaction.product_id).having(func.count() >= 5))).all()
    for pid, n in rep:
        p = names.get(pid)
        if p is not None:
            out.append(_a(f"adjust:{pid}:{now.isocalendar()[1]}", "repeated_adjustments", "low", p,
                          f"'{p.name}' stock adjusted {n} times in 7 days", "Frequent manual corrections can indicate a counting problem.",
                          {"adjustments_7d": n}, "Review the inventory transaction log."))

    # pages that returned 404 in the latest crawl for a product's source page
    gone = (await db.execute(select(Product, CrawledPage.status_code).join(CrawledPage, CrawledPage.id == Product.source_page_id)
                             .where(*live_products(business_id), CrawledPage.status_code.in_((404, 410))))).all()
    for p, code in gone:
        out.append(_a(f"page404:{p.id}", "page_gone", "medium", p, f"'{p.name}' source page returned HTTP {code}",
                      "The product page could not be fetched.", {"status_code": code, "url": p.source_url},
                      "Update the product URL or archive the product."))
    return out


async def scan(db: AsyncSession, business_id: uuid.UUID, extra: list[dict] | None = None) -> dict:
    found = {a["fingerprint"]: a for a in await detect(db, business_id) + (extra or [])}
    existing = {a.fingerprint: a for a in (await db.execute(select(ProductAnomaly).where(
        ProductAnomaly.business_id == business_id))).scalars()}
    now = now_utc()
    created = 0
    for fp, a in found.items():
        row = existing.get(fp)
        if row is None:
            db.add(ProductAnomaly(business_id=business_id, product_id=a["product_id"], fingerprint=fp, rule=a["rule"],
                                  severity=a["severity"], title=a["title"][:300], detail=a["detail"], evidence=a["evidence"],
                                  suggestion=a["suggestion"], status="open", detected_at=now, last_seen_at=now))
            created += 1
        else:
            row.last_seen_at, row.title, row.detail = now, a["title"][:300], a["detail"]
            if row.evidence != a["evidence"] and row.status != "open":
                row.status, row.resolved_at = "open", None  # evidence changed: surface it again
            row.evidence = a["evidence"]
    resolved = 0
    for fp, row in existing.items():
        if fp not in found and row.status == "open" and row.rule != "broken_image":
            row.status, row.resolved_at = "resolved", now
            resolved += 1
    await db.commit()
    return {"detected": len(found), "new": created, "resolved": resolved}


async def check_images(db: AsyncSession, business_id: uuid.UUID, limit: int = 40) -> list[dict]:
    """On demand: fetch product image URLs through the SSRF-safe fetcher and report unreachable ones."""
    from app.core.config import get_settings as app_settings
    from app.services.crawler import fetcher as fetcher_mod
    from app.services.crawler.fetcher import BlockedURL, SafeFetcher

    products = list((await db.execute(select(Product).where(*live_products(business_id)))).scalars())
    fetcher = SafeFetcher(app_settings(), transport=fetcher_mod.TRANSPORT_OVERRIDE, resolver=fetcher_mod.RESOLVER_OVERRIDE)
    out, checked = [], 0
    try:
        for p in products:
            for img in (p.images or [])[:2]:
                url = img.get("url")
                if not url or checked >= limit:
                    continue
                checked += 1
                problem = None
                try:
                    res = await fetcher.fetch(url, accept=("image/",))
                    if res.status_code is not None and res.status_code >= 400:
                        problem = f"HTTP {res.status_code}"
                    elif res.error:
                        problem = res.error
                except BlockedURL as e:
                    problem = f"blocked: {e}"
                except Exception as e:  # noqa: BLE001
                    problem = type(e).__name__
                if problem:
                    out.append(_a(f"img:{p.id}:{url[:120]}", "broken_image", "medium", p, f"An image of '{p.name}' is not reachable",
                                  f"{url} → {problem}", {"url": url, "problem": problem, "checked_at": now_utc().isoformat()},
                                  "Replace or remove the image URL."))
    finally:
        await fetcher.aclose()
    return out


def anomaly_dict(a: ProductAnomaly, name: str | None = None) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(a.id), "product_id": str(a.product_id) if a.product_id else None, "product_name": name, "rule": a.rule,
            "rule_label": RULES.get(a.rule, a.rule), "severity": a.severity, "title": a.title, "detail": a.detail,
            "evidence": a.evidence, "suggestion": a.suggestion, "status": a.status, "detected_at": iso(a.detected_at),
            "last_seen_at": iso(a.last_seen_at), "resolved_at": iso(a.resolved_at)}


# ------------------------------------------------------------------ price & inventory insights
async def pricing_insights(db: AsyncSession, business_id: uuid.UUID, days: int = 30) -> dict:
    now = now_utc()
    products = {p.id: p for p in (await db.execute(select(Product).where(*live_products(business_id)))).scalars()}
    recent = (await db.execute(select(PriceHistory).where(PriceHistory.business_id == business_id,
                                                          PriceHistory.created_at >= now - timedelta(days=days))
                               .order_by(PriceHistory.created_at.desc()).limit(200))).scalars().all()
    scheduled = (await db.execute(select(ScheduledPriceChange).where(ScheduledPriceChange.business_id == business_id,
                                                                     ScheduledPriceChange.status == "scheduled")
                                  .order_by(ScheduledPriceChange.effective_at))).scalars().all()
    ending = [p for p in products.values() if p.sale_price_amount is not None and p.sale_ends_at
              and now <= aware(p.sale_ends_at) <= now + timedelta(days=7)]
    invalid = [p for p in products.values() if (p.price_amount is not None and not p.currency) or
               (p.sale_price_amount is not None and p.price_amount is not None and p.sale_price_amount >= p.price_amount)]
    conflicts = (await db.execute(select(SyncConflict).where(SyncConflict.business_id == business_id, SyncConflict.status == "open",
                                                             SyncConflict.field.in_(("price", "price_amount", "currency"))))).scalars().all()
    daily: dict[str, Counter] = defaultdict(Counter)
    for h in recent:
        daily[aware(h.created_at).date().isoformat()][h.change_type] += 1
    ref = lambda p: {"id": str(p.id), "name": p.name}  # noqa: E731
    return {
        "period_days": days,
        "recent_changes": [{"product": ref(products[h.product_id]) if h.product_id in products else {"id": str(h.product_id), "name": None},
                            "price_type": h.price_type, "old": fmt(h.old_amount), "new": fmt(h.new_amount), "currency": h.currency,
                            "change_type": h.change_type, "actor": h.actor_label, "at": aware(h.created_at).isoformat()}
                           for h in recent[:50]],
        "scheduled_changes": [{"product": ref(products[c.product_id]) if c.product_id in products else None, "price_type": c.price_type,
                               "new": fmt(c.new_amount), "currency": c.currency, "effective_at": aware(c.effective_at).isoformat()}
                              for c in scheduled[:50]],
        "sales_ending_soon": [{**ref(p), "sale_price": fmt(p.sale_price_amount), "ends_at": aware(p.sale_ends_at).isoformat()} for p in ending],
        "invalid_prices": [{**ref(p), "price": fmt(p.price_amount), "sale_price": fmt(p.sale_price_amount), "currency": p.currency} for p in invalid],
        "website_price_conflicts": [{"product": ref(products[c.product_id]) if c.product_id in products else None, "field": c.field,
                                     "website": c.website_value, "managed": c.agentready_value} for c in conflicts],
        "daily_changes": [{"day": d, **c} for d, c in sorted(daily.items())],
        "currencies": sorted({p.currency for p in products.values() if p.currency}),
        "note": "Prices in different currencies are never compared or converted.",
    }


async def inventory_insights(db: AsyncSession, business_id: uuid.UUID, days: int = 90) -> dict:
    s = await get_settings(db, business_id)
    now = now_utc()
    products = list((await db.execute(select(Product).where(*live_products(business_id)))).scalars())
    tracked = [p for p in products if p.track_inventory]
    last_tx = dict((await db.execute(select(InventoryTransaction.product_id, func.max(InventoryTransaction.created_at)).where(
        InventoryTransaction.business_id == business_id).group_by(InventoryTransaction.product_id))).all())
    txs = (await db.execute(select(InventoryTransaction).where(InventoryTransaction.business_id == business_id,
                                                               InventoryTransaction.created_at >= now - timedelta(days=days))
                            .order_by(InventoryTransaction.created_at))).scalars().all()
    by_type: dict[str, Counter] = defaultdict(Counter)
    per_product: Counter = Counter()
    for t in txs:
        by_type[aware(t.created_at).date().isoformat()][t.transaction_type] += 1
        per_product[t.product_id] += 1
    ref = lambda p: {"id": str(p.id), "name": p.name, "sku": p.sku}  # noqa: E731
    stale = [p for p in tracked if (now - aware(last_tx.get(p.id) or p.created_at)).days > s.stale_stock_days]
    names = {p.id: p for p in products}
    sale_types = {"sale", "order", "fulfilment", "fulfillment"}
    order_tx = sum(1 for t in txs if (t.transaction_type or "") in sale_types)
    return {
        "period_days": days,
        "tracked": len(tracked), "untracked": len(products) - len(tracked),
        "low_stock": [{**ref(p), "status": p.stock_status} for p in tracked if p.stock_status == "low_stock"],
        "out_of_stock": [{**ref(p), "status": p.stock_status} for p in tracked if p.stock_status in ("out_of_stock", "backorder")],
        "stale_stock": [{**ref(p), "last_change": aware(last_tx[p.id]).isoformat() if p.id in last_tx else None} for p in stale],
        "tracking_disabled": [ref(p) for p in products if not p.track_inventory][:100],
        "repeated_adjustments": [{**ref(names[pid]), "adjustments": n} for pid, n in per_product.most_common(10)
                                 if n >= 3 and pid in names],
        "daily_transactions": [{"day": d, **c} for d, c in sorted(by_type.items())],
        "forecast": {
            "available": False,
            "reason": "Insufficient demand data: AgentReady does not receive customer orders, and manual stock adjustments are "
                      "not sales. Connect order data (future phase) to enable forecasting."
                      if order_tx == 0 else f"Only {order_tx} order-type transactions recorded; at least 30 days of order "
                                            "history are needed.",
            "order_transactions": order_tx, "adjustment_transactions": len(txs) - order_tx,
        },
    }
