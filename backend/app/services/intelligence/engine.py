"""Quality refresh (incremental), daily snapshots and catalog/category aggregates.

Incremental: a product is re-evaluated only when it has no result yet, or it (or one of its variants) changed after
its result was computed, or the merchant changed quality/distribution settings, or the result is older than a day
(freshness depends on the passage of time). Everything else is served from ``product_quality_results``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (AttributeDefinition, Business, CatalogQualitySnapshot, Category, IntelligenceSettings,
                        InventoryTransaction, PriceHistory, Product, ProductEvidence, ProductQualityResult,
                        ProductVariant, ValidationIssue, live_products)
from app.services.commerce.money import aware, now_utc
from app.services.distribution.profile import get_profile, merchant_scope
from app.services.intelligence.quality import DEFAULT_WEIGHTS, DIMENSIONS, QualityContext, evaluate

RECOMPUTE_AFTER = timedelta(hours=24)
BATCH = 300


async def get_settings(db: AsyncSession, business_id: uuid.UUID) -> IntelligenceSettings:
    s = (await db.execute(select(IntelligenceSettings).where(IntelligenceSettings.business_id == business_id)
                          .execution_options(populate_existing=True))).scalar_one_or_none()
    if s is None:
        from sqlalchemy.exc import IntegrityError

        try:
            async with db.begin_nested():  # another request/worker may create the row concurrently
                s = IntelligenceSettings(business_id=business_id, quality_weights={}, category_requirements={}, alerts={})
                db.add(s)
        except IntegrityError:
            s = (await db.execute(select(IntelligenceSettings).where(IntelligenceSettings.business_id == business_id)
                                  .execution_options(populate_existing=True))).scalar_one()
    return s


async def build_context(db: AsyncSession, business: Business, product_ids: list[uuid.UUID]) -> QualityContext:
    s = await get_settings(db, business.id)
    prof = await get_profile(db, business)
    scope = await merchant_scope(db, business, prof)
    cats = {c.id: c for c in (await db.execute(select(Category).where(Category.business_id == business.id))).scalars()}
    ancestors: dict = {}
    for cid in cats:
        out, cur = set(), cats.get(cid)
        while cur is not None and cur.id not in out:
            out.add(cur.id)
            cur = cats.get(cur.parent_id) if cur.parent_id else None
        ancestors[cid] = out
    defs = list((await db.execute(select(AttributeDefinition).where(
        AttributeDefinition.business_id == business.id, AttributeDefinition.archived_at.is_(None)))).scalars())
    evidence: dict = defaultdict(dict)
    rank = {"verified": 4, "merchant": 3, "deterministic": 2, "ai": 1}
    for pid, field, method, verified in (await db.execute(select(
            ProductEvidence.product_id, ProductEvidence.field, ProductEvidence.extraction_method,
            ProductEvidence.verified_in_source).where(ProductEvidence.business_id == business.id,
                                                       ProductEvidence.product_id.in_(product_ids)))).all():
        kind = "merchant" if method == "merchant" else "verified" if verified else "ai" if method.startswith("ai") else "deterministic"
        if rank[kind] > rank.get(evidence[pid].get(field, ""), 0):
            evidence[pid][field] = kind
    errors = dict((await db.execute(select(ValidationIssue.product_id, func.count()).where(
        ValidationIssue.business_id == business.id, ValidationIssue.status == "open", ValidationIssue.severity == "error",
        ValidationIssue.product_id.in_(product_ids)).group_by(ValidationIssue.product_id))).all())
    prices = dict((await db.execute(select(PriceHistory.product_id, func.max(PriceHistory.created_at)).where(
        PriceHistory.business_id == business.id, PriceHistory.product_id.in_(product_ids))
        .group_by(PriceHistory.product_id))).all())
    stock = dict((await db.execute(select(InventoryTransaction.product_id, func.max(InventoryTransaction.created_at)).where(
        InventoryTransaction.business_id == business.id, InventoryTransaction.product_id.in_(product_ids))
        .group_by(InventoryTransaction.product_id))).all())
    return QualityContext(scope=scope, definitions=defs, ancestors=ancestors,
                          category_requirements=s.category_requirements or {}, evidence=evidence, open_errors=errors,
                          last_price_change={k: aware(v) for k, v in prices.items()},
                          last_stock_change={k: aware(v) for k, v in stock.items()},
                          weights={**DEFAULT_WEIGHTS, **(s.quality_weights or {})}, stale_after_days=s.stale_after_days,
                          stale_price_days=s.stale_price_days, stale_stock_days=s.stale_stock_days)


async def stale_product_ids(db: AsyncSession, business: Business, force: bool = False) -> list[uuid.UUID]:
    if force:
        return list((await db.execute(select(Product.id).where(*live_products(business.id)))).scalars())
    from app.models import DistributionProfile

    await get_settings(db, business.id)
    await get_profile(db, business)
    s_at = (await db.execute(select(IntelligenceSettings.updated_at).where(IntelligenceSettings.business_id == business.id))).scalar_one()
    p_at = (await db.execute(select(DistributionProfile.updated_at).where(DistributionProfile.business_id == business.id))).scalar_one()
    settings_changed = max(aware(s_at), aware(p_at))
    cutoff = now_utc() - RECOMPUTE_AFTER
    variant_changed = (select(func.max(ProductVariant.updated_at)).where(ProductVariant.product_id == Product.id)
                       .correlate(Product).scalar_subquery())
    stmt = (select(Product.id).outerjoin(ProductQualityResult, ProductQualityResult.product_id == Product.id)
            .where(*live_products(business.id), or_(
                ProductQualityResult.id.is_(None), ProductQualityResult.computed_at < Product.updated_at,
                ProductQualityResult.computed_at < variant_changed, ProductQualityResult.computed_at < settings_changed,
                ProductQualityResult.computed_at < cutoff, ProductQualityResult.product_version != Product.version)))
    return list((await db.execute(stmt)).scalars())


async def refresh(db: AsyncSession, business: Business, force: bool = False) -> dict:
    """Re-evaluate changed products only. Returns {'evaluated': n, 'total': m}."""
    ids = await stale_product_ids(db, business, force)
    # products deleted/removed since: drop their results
    gone = (await db.execute(select(ProductQualityResult).join(Product, Product.id == ProductQualityResult.product_id).where(
        ProductQualityResult.business_id == business.id,
        or_(Product.deleted_at.is_not(None), Product.is_removed.is_(True))))).scalars().all()
    for r in gone:
        await db.delete(r)
    done = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        ctx = await build_context(db, business, chunk)
        rows = (await db.execute(select(Product).where(Product.id.in_(chunk)))).scalars().all()
        existing = {r.product_id: r for r in (await db.execute(select(ProductQualityResult).where(
            ProductQualityResult.product_id.in_(chunk)))).scalars()}
        for p in rows:
            res = evaluate(p, ctx)
            r = existing.get(p.id) or ProductQualityResult(business_id=business.id, product_id=p.id)
            for k in ("score", "level", "dimensions", "missing_required", "missing_optional", "invalid", "stale", "blockers",
                      "withheld", "suggestions"):
                setattr(r, k, res[k])
            r.category_id, r.product_version, r.computed_at = p.category_id, p.version, now_utc()
            if p.id not in existing:
                db.add(r)
            done += 1
        await db.commit()
    total = (await db.execute(select(func.count()).select_from(ProductQualityResult).where(
        ProductQualityResult.business_id == business.id))).scalar_one()
    return {"evaluated": done, "total": total}


async def results(db: AsyncSession, business_id: uuid.UUID, *, with_relations: bool = False
                  ) -> list[tuple[ProductQualityResult, Product]]:
    """Quality results joined to their products. Attributes/variants are NOT loaded unless asked for: on a
    5,000-product catalog eager-loading them made every caller ~1.5 s slower. Accessing them without
    ``with_relations=True`` raises instead of silently returning empty lists."""
    from sqlalchemy.orm import raiseload, selectinload

    opts = ([selectinload(Product.attributes), selectinload(Product.variants)] if with_relations
            else [raiseload(Product.attributes), raiseload(Product.variants)])
    return list((await db.execute(select(ProductQualityResult, Product).join(Product, Product.id == ProductQualityResult.product_id)
                                  .where(ProductQualityResult.business_id == business_id, *live_products(business_id))
                                  .options(*opts))).all())


def summarize(rows: list[tuple[ProductQualityResult, Product]]) -> dict:
    n = len(rows)
    levels = defaultdict(int)
    dims: dict[str, list[float]] = defaultdict(list)
    cats: dict[str, list[float]] = defaultdict(list)
    cat_dims: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    missing_counts: dict[str, int] = defaultdict(int)
    for r, p in rows:
        levels[r.level] += 1
        path = p.category or "Uncategorised"
        cats[path].append(r.score)
        for k, v in (r.dimensions or {}).items():
            if v.get("score") is not None:
                dims[k].append(v["score"])
                cat_dims[path][k].append(v["score"])
        for f in r.missing_required or []:
            missing_counts[f] += 1
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "products": n, "average_score": avg([r.score for r, _ in rows]),
        "levels": {k: levels.get(k, 0) for k in ("excellent", "good", "needs_improvement", "critical")},
        "dimensions": {k: {"label": DIMENSIONS[k], "average": avg(dims.get(k, [])), "products": len(dims.get(k, []))}
                       for k in DIMENSIONS},
        "categories": sorted([{"category": c, "products": len(v), "average": avg(v),
                               "dimensions": {k: avg(x) for k, x in cat_dims[c].items()}} for c, v in cats.items()],
                             key=lambda x: (x["average"] or 0)),
        "missing_required": sorted(({"field": f, "products": c} for f, c in missing_counts.items()),
                                   key=lambda x: -x["products"]),
        "attention": sum(1 for r, _ in rows if r.level in ("critical", "needs_improvement")),
        "stale": sum(1 for r, _ in rows if r.stale),
        "missing_specifications": sum(1 for r, _ in rows if any(f.startswith("attribute:") or f == "attributes"
                                                                for f in (r.missing_required or []) + (r.missing_optional or []))),
        "distribution_ready": sum(1 for r, _ in rows if (r.dimensions or {}).get("distribution", {}).get("score") == 100.0),
    }


async def snapshot(db: AsyncSession, business: Business, force: bool = False) -> CatalogQualitySnapshot | None:
    """At most one snapshot per merchant per UTC day (re-taken when forced)."""
    day = now_utc().date().isoformat()
    snap = (await db.execute(select(CatalogQualitySnapshot).where(CatalogQualitySnapshot.business_id == business.id,
                                                                  CatalogQualitySnapshot.day == day))).scalar_one_or_none()
    if snap is not None and not force:
        return snap
    summ = summarize(await results(db, business.id))
    if snap is None:
        snap = CatalogQualitySnapshot(business_id=business.id, day=day)
        db.add(snap)
    snap.products, snap.average_score, snap.levels = summ["products"], summ["average_score"], summ["levels"]
    snap.dimensions = {k: v["average"] for k, v in summ["dimensions"].items()}
    snap.categories = {c["category"]: {"products": c["products"], "average": c["average"]} for c in summ["categories"]}
    snap.distribution_ready, snap.stale_products, snap.attention_products = (summ["distribution_ready"], summ["stale"],
                                                                             summ["attention"])
    s = await get_settings(db, business.id)
    s.last_snapshot_day = day
    await db.commit()
    return snap


async def history(db: AsyncSession, business_id: uuid.UUID, days: int = 90) -> list[dict]:
    since = (now_utc() - timedelta(days=days)).date().isoformat()
    rows = (await db.execute(select(CatalogQualitySnapshot).where(CatalogQualitySnapshot.business_id == business_id,
                                                                  CatalogQualitySnapshot.day >= since)
                             .order_by(CatalogQualitySnapshot.day))).scalars().all()
    return [{"day": r.day, "products": r.products, "average_score": r.average_score, "levels": r.levels,
             "dimensions": r.dimensions, "distribution_ready": r.distribution_ready, "stale_products": r.stale_products,
             "attention_products": r.attention_products, "categories": r.categories} for r in rows]


def compare_periods(hist: list[dict], days: int) -> dict:
    """Latest snapshot vs the latest snapshot at least ``days`` older. Coverage is reported, never guessed."""
    if not hist:
        return {"available": False, "reason": "No quality snapshots yet"}
    cur = hist[-1]
    target = (now_utc() - timedelta(days=days)).date().isoformat()
    older = [h for h in hist if h["day"] <= target]
    if not older:
        return {"available": False, "reason": f"History starts {hist[0]['day']}; not enough coverage for a {days}-day comparison",
                "current": cur}
    prev = older[-1]
    delta = lambda a, b: None if a is None or b is None else round(a - b, 1)  # noqa: E731
    return {"available": True, "current": cur, "previous": prev,
            "change": {"average_score": delta(cur["average_score"], prev["average_score"]),
                       "distribution_ready": cur["distribution_ready"] - prev["distribution_ready"],
                       "stale_products": cur["stale_products"] - prev["stale_products"],
                       "attention_products": cur["attention_products"] - prev["attention_products"]}}


def result_dict(r: ProductQualityResult, p: Product | None = None) -> dict:
    return {"product_id": str(r.product_id), "name": p.name if p else None, "sku": p.sku if p else None,
            "category": p.category if p else None, "score": r.score, "level": r.level, "dimensions": r.dimensions,
            "missing_required": r.missing_required, "missing_optional": r.missing_optional, "invalid": r.invalid,
            "stale": r.stale, "blockers": r.blockers, "withheld": r.withheld, "suggestions": r.suggestions,
            "computed_at": aware(r.computed_at).isoformat()}
