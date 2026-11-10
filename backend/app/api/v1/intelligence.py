"""Commerce Intelligence API (merchant-scoped). Numbers come from SQL/deterministic services; Groq only phrases answers
and drafts content, and nothing changes catalog data without an explicit merchant approval."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.core.errors import AppError, not_found
from app.core.ratelimit import hit
from app.db.session import get_db
from app.models import (AIConversation, AIConversationMessage, Category, IntelligenceRecommendation, IntelligenceReport,
                        MerchantNotification, PriceHistory, Product, ProductAnomaly, ProductQualityResult,
                        SuggestedProductEdit, SyncConflict, live_products)
from app.services.commerce.audit import actor_from_ctx, record
from app.services.commerce.money import aware, now_utc
from app.services.intelligence import anomalies, assistant, discovery, engine, notifications, recommendations, reports
from app.services.intelligence.quality import DEFAULT_WEIGHTS, DIMENSIONS, LEVELS

router = APIRouter(prefix="/intelligence", tags=["commerce intelligence"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsIn(Strict):
    quality_weights: dict[str, float] | None = None
    category_requirements: dict[str, list[str]] | None = None
    stale_after_days: int | None = Field(None, ge=1, le=3650)
    stale_price_days: int | None = Field(None, ge=1, le=3650)
    stale_stock_days: int | None = Field(None, ge=1, le=3650)
    price_change_alert_percent: float | None = Field(None, gt=0, le=1000)
    alerts: dict[str, dict] | None = None
    notification_frequency: Literal["immediate", "daily"] | None = None


class ApplyIn(Strict):
    confirm: bool = False
    use_ai: bool = False


class ReportIn(Strict):
    kind: Literal[tuple(reports.KINDS)]  # type: ignore[valid-type]
    format: Literal["pdf", "csv", "json"] = "pdf"
    days: int = Field(30, ge=1, le=365)


class QueryIn(Strict):
    question: str = Field(min_length=2, max_length=1000)
    conversation_id: str | None = None
    use_ai: bool = True


class SuggestIn(Strict):
    product_id: str
    kind: Literal["description", "attributes"] = "description"
    use_ai: bool = True


class ApproveIn(Strict):
    attribute_keys: list[str] | None = None  # attributes: approve a subset
    description: str | None = Field(None, max_length=20000)  # the merchant may edit the proposal before approving


def _uuid(v, what):
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise not_found(what)


async def _fresh(db, ctx):
    await engine.refresh(db, ctx.business)
    await engine.snapshot(db, ctx.business)


# ================================================================ overview & settings
@router.get("/overview")
async def overview(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    await _fresh(db, ctx)
    bid = ctx.business_id
    rows = await engine.results(db, bid)
    summ = engine.summarize(rows)
    hist = await engine.history(db, bid, 90)
    status = Counter(f"{p.lifecycle_status}/{p.approval_status}/{p.publication_status}" for _, p in rows)
    lifecycle = Counter(p.lifecycle_status for _, p in rows)
    approval = Counter(p.approval_status for _, p in rows)
    now = now_utc()
    buckets = Counter()
    for _, p in rows:
        age = (now - aware(p.updated_at)).days
        buckets["<30 days" if age < 30 else "30-90 days" if age < 90 else "90-180 days" if age < 180 else ">180 days"] += 1
    price_changes = (await db.execute(select(func.count()).select_from(PriceHistory).where(
        PriceHistory.business_id == bid, PriceHistory.created_at >= now - timedelta(days=7),
        PriceHistory.change_type != "product_created"))).scalar_one()
    conflicts = (await db.execute(select(func.count()).select_from(SyncConflict).where(
        SyncConflict.business_id == bid, SyncConflict.status == "open"))).scalar_one()
    stock = Counter(p.stock_status for _, p in rows if p.track_inventory)
    ins = await discovery.search_insights(db, bid, days)
    interest = await discovery.product_interest(db, bid, days, 10)
    recs = (await db.execute(select(IntelligenceRecommendation).where(IntelligenceRecommendation.business_id == bid,
                                                                       IntelligenceRecommendation.status == "open"))).scalars().all()
    anomalies_open = (await db.execute(select(func.count()).select_from(ProductAnomaly).where(
        ProductAnomaly.business_id == bid, ProductAnomaly.status == "open"))).scalar_one()
    recs.sort(key=lambda r: recommendations.ORDER[r.priority])
    return {
        "catalog": {"kind": "catalog", "products": summ["products"], "average_score": summ["average_score"], "levels": summ["levels"],
                    "attention": summ["attention"], "stale": summ["stale"], "missing_specifications": summ["missing_specifications"],
                    "distribution_ready": summ["distribution_ready"], "dimensions": summ["dimensions"],
                    "categories": summ["categories"], "trend": hist, "freshness": [{"bucket": b, "products": buckets.get(b, 0)}
                                                                                for b in ("<30 days", "30-90 days", "90-180 days", ">180 days")],
                    "status": {"lifecycle": dict(lifecycle), "approval": dict(approval), "combined": dict(status)}},
        "commerce": {"kind": "commerce", "price_changes_7d": price_changes, "open_conflicts": conflicts,
                     "low_stock": stock.get("low_stock", 0), "out_of_stock": stock.get("out_of_stock", 0) + stock.get("backorder", 0),
                     "open_anomalies": anomalies_open},
        "observed": {"kind": "observed", "period_days": days, "searches": ins["searches"],
                     "product_requests": sum(p["product_detail"] for p in interest["products"]),
                     "top_products": interest["products"][:5], "top_categories": interest["categories"][:8],
                     "category_search_interest": ins["categories_requested"][:8],
                     "zero_result_queries": len(ins["zero_result_queries"]), "trend": await discovery.activity_trend(db, bid, days),
                     "disclaimer": discovery.DISCLAIMER},
        "inferred": {"kind": "inferred", "open_recommendations": len(recs),
                     "by_priority": dict(Counter(r.priority for r in recs)),
                     "top": [recommendations.rec_dict(r) for r in recs[:5]],
                     "note": "Recommendations are rule-based inferences from the data above; review before acting."},
        "not_measured": ["sales", "revenue", "orders", "conversions", "impressions", "purchases"],
    }


@router.get("/settings")
async def get_settings_(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await engine.get_settings(db, ctx.business_id)
    await db.commit()
    return _settings_dict(s)


def _settings_dict(s) -> dict:
    return {"quality_weights": {**DEFAULT_WEIGHTS, **(s.quality_weights or {})}, "default_weights": DEFAULT_WEIGHTS,
            "dimensions": DIMENSIONS, "levels": dict(LEVELS), "category_requirements": s.category_requirements or {},
            "stale_after_days": s.stale_after_days, "stale_price_days": s.stale_price_days, "stale_stock_days": s.stale_stock_days,
            "price_change_alert_percent": s.price_change_alert_percent, "alerts": notifications.alert_config(s),
            "notification_frequency": s.notification_frequency, "email": notifications.email_channel(),
            "last_evaluated_at": aware(s.last_evaluated_at).isoformat() if s.last_evaluated_at else None}


@router.put("/settings")
async def update_settings(body: SettingsIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    s = await engine.get_settings(db, ctx.business_id)
    d = body.model_dump(exclude_unset=True)
    if d.get("quality_weights") is not None:
        bad = [k for k in d["quality_weights"] if k not in DEFAULT_WEIGHTS]
        if bad or any(v < 0 or v > 100 for v in d["quality_weights"].values()):
            raise AppError(422, "invalid_weights", f"Weights must be 0-100 for: {', '.join(DEFAULT_WEIGHTS)}")
        if sum({**DEFAULT_WEIGHTS, **d["quality_weights"]}.values()) == 0:
            raise AppError(422, "invalid_weights", "At least one weight must be positive")
    if d.get("category_requirements") is not None:
        ids = [_uuid(k, "Category") for k in d["category_requirements"]]
        owned = set((await db.execute(select(Category.id).where(Category.business_id == ctx.business_id, Category.id.in_(ids)))).scalars())
        if len(owned) != len(set(ids)):
            raise not_found("Category")
        d["category_requirements"] = {k: sorted({x.strip().lower() for x in v if x.strip()}) for k, v in d["category_requirements"].items()}
    if d.get("alerts") is not None:
        bad = [k for k in d["alerts"] if k not in notifications.ALERTS]
        if bad:
            raise AppError(422, "invalid_alerts", f"Unknown alert type(s): {', '.join(bad)}")
        d["alerts"] = {k: {"enabled": bool(v.get("enabled", True)), "threshold": float(v.get("threshold", notifications.ALERTS[k]["threshold"]))}
                       for k, v in d["alerts"].items()}
    for k, v in d.items():
        if v is not None:
            setattr(s, k, v)
    s.updated_at = now_utc()  # invalidates stored quality results
    record(db, ctx.business_id, actor_from_ctx(ctx), "intelligence.settings_updated", "intelligence", s.id,
           "Commerce intelligence settings updated: " + ", ".join(sorted(d)), d)
    await db.commit()
    return _settings_dict(s)


@router.post("/refresh")
async def refresh(force: bool = False, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    out = await notifications.run_for_business(db, ctx.business, force=force)
    await engine.snapshot(db, ctx.business, force=True)
    return out


# ================================================================ quality
@router.get("/catalog-quality")
async def catalog_quality(level: str | None = Query(None, pattern="^(excellent|good|needs_improvement|critical)$"),
                          q: str | None = None, category: str | None = None,
                          filter: str | None = Query(None, pattern="^(stale|blockers|missing_specifications|invalid)$"),
                          sort: str = Query("score", pattern="^-?(score|name)$"), page: int = Query(1, ge=1),
                          page_size: int = Query(25, ge=1, le=200), ctx: AuthContext = Depends(current_auth),
                          db: AsyncSession = Depends(get_db)):
    await _fresh(db, ctx)
    rows = await engine.results(db, ctx.business_id)
    summ = engine.summarize(rows)
    sel = rows
    if level:
        sel = [x for x in sel if x[0].level == level]
    if q:
        sel = [x for x in sel if q.lower() in (x[1].name or "").lower() or q.lower() in (x[1].sku or "").lower()]
    if category:
        sel = [x for x in sel if (x[1].category or "Uncategorised") == category]
    if filter == "stale":
        sel = [x for x in sel if x[0].stale]
    elif filter == "blockers":
        sel = [x for x in sel if x[0].blockers]
    elif filter == "invalid":
        sel = [x for x in sel if x[0].invalid]
    elif filter == "missing_specifications":
        sel = [x for x in sel if any(f.startswith("attribute:") or f == "attributes" for f in x[0].missing_required + x[0].missing_optional)]
    key = (lambda x: x[0].score) if sort.lstrip("-") == "score" else (lambda x: (x[1].name or "").lower())
    sel.sort(key=key, reverse=sort.startswith("-"))
    hist = await engine.history(db, ctx.business_id, 90)
    return {"summary": summ, "history": hist, "comparisons": {"week": engine.compare_periods(hist, 7), "month": engine.compare_periods(hist, 30)},
            "methodology": {"formula": "score = 100 x sum(weight x dimension score) / sum(weights of applicable dimensions)",
                            "weights": {**DEFAULT_WEIGHTS, **((await engine.get_settings(db, ctx.business_id)).quality_weights or {})},
                            "dimensions": DIMENSIONS, "levels": dict(LEVELS),
                            "note": "Measures completeness, validity, freshness and provenance — not factual accuracy. Withheld "
                                    "values are not penalised; non-applicable dimensions are excluded."},
            "items": [engine.result_dict(r, p) for r, p in sel[(page - 1) * page_size: page * page_size]],
            "total": len(sel), "page": page, "page_size": page_size}


@router.get("/products/{product_id}/quality")
async def product_quality(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    pid = _uuid(product_id, "Product")
    p = (await db.execute(select(Product).where(Product.id == pid, *live_products(ctx.business_id)))).scalar_one_or_none()
    if p is None:
        raise not_found("Product")
    await engine.refresh(db, ctx.business)
    r = (await db.execute(select(ProductQualityResult).where(ProductQualityResult.product_id == pid))).scalar_one()
    from app.services.intelligence.quality import required_attribute_keys

    ctxq = await engine.build_context(db, ctx.business, [pid])
    required, _ = required_attribute_keys(p, ctxq)
    interest = await discovery.product_interest(db, ctx.business_id, 30, 1000)
    sig = next((x for x in interest["products"] if x["product_id"] == str(pid)), None)
    anoms = (await db.execute(select(ProductAnomaly).where(ProductAnomaly.product_id == pid, ProductAnomaly.status == "open"))).scalars().all()
    return {**engine.result_dict(r, p), "required_attributes": required,
            "attributes": [{"key": a.key, "label": a.label, "value": a.value, "unit": a.unit} for a in p.attributes],
            "agent_interest_30d": sig, "anomalies": [anomalies.anomaly_dict(a, p.name) for a in anoms]}


@router.get("/categories/quality")
async def categories_quality(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    await _fresh(db, ctx)
    rows = await engine.results(db, ctx.business_id)
    summ = engine.summarize(rows)
    missing_by_cat: dict[str, Counter] = {}
    for r, p in rows:
        c = missing_by_cat.setdefault(p.category or "Uncategorised", Counter())
        for f in r.missing_required:
            c[f] += 1
    s = await engine.get_settings(db, ctx.business_id)
    return {"items": [{**c, "missing": [{"field": f, "products": n} for f, n in missing_by_cat.get(c["category"], Counter()).most_common(10)]}
                      for c in summ["categories"]],
            "category_requirements": s.category_requirements or {},
            "note": "Requirements differ per category: required attribute definitions (Categories → Attributes) and the "
                    "category requirements configured here apply only to their category and its subcategories."}


# ================================================================ discovery
@router.get("/search-insights")
async def search_insights(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return await discovery.search_insights(db, ctx.business_id, days)


@router.get("/top-products")
async def top_products(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return await discovery.product_interest(db, ctx.business_id, days)


@router.get("/zero-result-queries")
async def zero_results(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return {"items": await discovery.zero_result_opportunities(db, ctx.business_id, days), "period_days": days,
            "note": "Findings explain why nothing was returned; they never assume you sell what was requested."}


@router.get("/activity")
async def activity(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return {"items": await discovery.activity_trend(db, ctx.business_id, days), "disclaimer": discovery.DISCLAIMER}


# ================================================================ recommendations
async def _rec(db, ctx, rec_id) -> IntelligenceRecommendation:
    r = (await db.execute(select(IntelligenceRecommendation).where(IntelligenceRecommendation.id == _uuid(rec_id, "Recommendation"),
                                                                   IntelligenceRecommendation.business_id == ctx.business_id))).scalar_one_or_none()
    if r is None:
        raise not_found("Recommendation")
    return r


@router.get("/recommendations")
async def list_recs(status: str = Query("open", pattern="^(open|dismissed|applied|resolved|all)$"),
                    priority: str | None = Query(None, pattern="^(critical|high|medium|low)$"), refresh_: bool = Query(False, alias="refresh"),
                    ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    await engine.refresh(db, ctx.business)
    existing = (await db.execute(select(func.count()).select_from(IntelligenceRecommendation).where(
        IntelligenceRecommendation.business_id == ctx.business_id))).scalar_one()
    if refresh_ or not existing:
        await anomalies.scan(db, ctx.business_id)
        await recommendations.refresh(db, ctx.business)
    stmt = select(IntelligenceRecommendation).where(IntelligenceRecommendation.business_id == ctx.business_id)
    if status != "all":
        stmt = stmt.where(IntelligenceRecommendation.status == status)
    if priority:
        stmt = stmt.where(IntelligenceRecommendation.priority == priority)
    rows = list((await db.execute(stmt)).scalars())
    rows.sort(key=lambda r: (recommendations.ORDER[r.priority], -len(r.product_ids or [])))
    return {"items": [recommendations.rec_dict(r) for r in rows], "priority_rules": recommendations.PRIORITY_RULES}


@router.get("/recommendations/{rec_id}")
async def get_rec(rec_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return recommendations.rec_dict(await _rec(db, ctx, rec_id))


@router.post("/recommendations/{rec_id}/dismiss")
async def dismiss_rec(rec_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    r = await _rec(db, ctx, rec_id)
    r.status, r.dismissed_at, r.decided_by = "dismissed", now_utc(), ctx.user.email if ctx.user else None
    record(db, ctx.business_id, actor_from_ctx(ctx), "intelligence.recommendation_dismissed", "recommendation", r.id, f"Dismissed: {r.title}")
    await db.commit()
    return recommendations.rec_dict(r)


@router.post("/recommendations/{rec_id}/reopen")
async def reopen_rec(rec_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    r = await _rec(db, ctx, rec_id)
    r.status, r.dismissed_at = "open", None
    await db.commit()
    return recommendations.rec_dict(r)


@router.post("/recommendations/{rec_id}/apply")
async def apply_rec(rec_id: str, body: ApplyIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    r = await _rec(db, ctx, rec_id)
    if r.status != "open":
        raise AppError(409, "not_open", "Only open recommendations can be applied")
    if not r.action:
        raise AppError(409, "manual_action", "This recommendation needs changes only you can make (e.g. adding verified "
                                             "specifications). Follow the suggested action, then refresh.")
    if not body.confirm:
        raise AppError(422, "confirmation_required", "Confirm the change explicitly (confirm=true)")
    ids = [uuid.UUID(i) for i in r.product_ids or []]
    products = list((await db.execute(select(Product).where(Product.id.in_(ids), *live_products(ctx.business_id)))).scalars())
    actor = actor_from_ctx(ctx)
    result: dict = {}
    if r.action == "assign_category":
        from app.services.commerce.catalog_rules import assign_category

        cats = {c.path.lower(): c for c in (await db.execute(select(Category).where(Category.business_id == ctx.business_id,
                                                                                    Category.archived_at.is_(None)))).scalars()}
        n = 0
        for p in products:
            c = cats.get((p.category or "").lower())
            if c is not None and p.category_id is None:
                await assign_category(db, p, c, actor)
                n += 1
        result = {"assigned": n}
    elif r.action == "draft_descriptions":
        made = []
        for p in products[:10]:
            sug = await assistant.suggest_description(db, ctx.business, p, use_ai=body.use_ai)
            made.append(str(await _store_suggestion(db, ctx, p, "description", sug)))
        result = {"suggestions_created": len(made), "suggestion_ids": made,
                  "note": "Drafts are pending your review in the AI assistant; nothing was changed yet."}
    r.status, r.applied_at, r.decided_by = "applied", now_utc(), ctx.user.email if ctx.user else None
    record(db, ctx.business_id, actor, "intelligence.recommendation_applied", "recommendation", r.id, f"Applied: {r.title}", result)
    await db.commit()
    return {**recommendations.rec_dict(r), "result": result}


# ================================================================ anomalies & insights
@router.get("/anomalies")
async def list_anomalies(status: str = Query("open", pattern="^(open|resolved|dismissed|all)$"), rule: str | None = None,
                         severity: str | None = Query(None, pattern="^(high|medium|low)$"), refresh_: bool = Query(False, alias="refresh"),
                         ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    if refresh_:
        await anomalies.scan(db, ctx.business_id)
    stmt = select(ProductAnomaly, Product.name).outerjoin(Product, Product.id == ProductAnomaly.product_id).where(
        ProductAnomaly.business_id == ctx.business_id)
    if status != "all":
        stmt = stmt.where(ProductAnomaly.status == status)
    if rule:
        stmt = stmt.where(ProductAnomaly.rule == rule)
    if severity:
        stmt = stmt.where(ProductAnomaly.severity == severity)
    rows = (await db.execute(stmt.order_by(ProductAnomaly.detected_at.desc()).limit(500))).all()
    order = {"high": 0, "medium": 1, "low": 2}
    items = sorted((anomalies.anomaly_dict(a, n) for a, n in rows), key=lambda x: order[x["severity"]])
    return {"items": items, "rules": anomalies.RULES,
            "note": "Detection never changes your data. Rules are deterministic; see each item's evidence."}


@router.get("/anomalies/{anomaly_id}")
async def get_anomaly(anomaly_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ProductAnomaly, Product.name).outerjoin(Product, Product.id == ProductAnomaly.product_id).where(
        ProductAnomaly.id == _uuid(anomaly_id, "Anomaly"), ProductAnomaly.business_id == ctx.business_id))).first()
    if row is None:
        raise not_found("Anomaly")
    return anomalies.anomaly_dict(*row)


@router.post("/anomalies/{anomaly_id}/dismiss")
async def dismiss_anomaly(anomaly_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    a = (await db.execute(select(ProductAnomaly).where(ProductAnomaly.id == _uuid(anomaly_id, "Anomaly"),
                                                       ProductAnomaly.business_id == ctx.business_id))).scalar_one_or_none()
    if a is None:
        raise not_found("Anomaly")
    a.status, a.resolved_at = "dismissed", now_utc()
    await db.commit()
    return anomalies.anomaly_dict(a)


@router.post("/anomalies/check-images")
async def check_images(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    if not await hit(f"imgcheck:{ctx.business_id}", 3):
        raise AppError(429, "rate_limited", "Image checks are limited to 3 per minute")
    found = await anomalies.check_images(db, ctx.business_id)
    out = await anomalies.scan(db, ctx.business_id, extra=found)
    return {**out, "broken_images": len(found)}


@router.get("/pricing-insights")
async def pricing_insights(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return await anomalies.pricing_insights(db, ctx.business_id, days)


@router.get("/inventory-insights")
async def inventory_insights(days: int = Query(90, ge=1, le=365), ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return await anomalies.inventory_insights(db, ctx.business_id, days)


# ================================================================ reports
@router.post("/reports", status_code=201)
async def create_report(body: ReportIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rep = await reports.generate(db, ctx.business, body.kind, body.format, body.days, ctx.user.email if ctx.user else None)
    record(db, ctx.business_id, actor_from_ctx(ctx), "intelligence.report_generated", "report", rep.id, f"{rep.title} ({rep.format}) generated")
    await db.commit()
    return reports.report_dict(rep)


@router.get("/reports")
async def list_reports(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(IntelligenceReport).where(IntelligenceReport.business_id == ctx.business_id)
                             .order_by(IntelligenceReport.created_at.desc()).limit(100))).scalars().all()
    return {"items": [reports.report_dict(r) for r in rows], "kinds": reports.KINDS, "formats": list(reports.FORMATS)}


async def _report(db, ctx, rid) -> IntelligenceReport:
    r = (await db.execute(select(IntelligenceReport).where(IntelligenceReport.id == _uuid(rid, "Report"),
                                                           IntelligenceReport.business_id == ctx.business_id))).scalar_one_or_none()
    if r is None:
        raise not_found("Report")
    return r


@router.get("/reports/{report_id}")
async def get_report(report_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return reports.report_dict(await _report(db, ctx, report_id))


@router.get("/reports/{report_id}/download")
async def download_report(report_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    r = await _report(db, ctx, report_id)
    return Response(content=r.data or b"", media_type=r.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{r.filename}"'})


# ================================================================ notifications
@router.get("/notifications")
async def list_notifications(status: str = Query("active", pattern="^(active|unread|read|dismissed|all)$"),
                             ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    stmt = select(MerchantNotification).where(MerchantNotification.business_id == ctx.business_id)
    if status == "active":
        stmt = stmt.where(MerchantNotification.status != "dismissed")
    elif status != "all":
        stmt = stmt.where(MerchantNotification.status == status)
    rows = (await db.execute(stmt.order_by(MerchantNotification.last_occurred_at.desc()).limit(200))).scalars().all()
    unread = (await db.execute(select(func.count()).select_from(MerchantNotification).where(
        MerchantNotification.business_id == ctx.business_id, MerchantNotification.status == "unread"))).scalar_one()
    return {"items": [notifications.notification_dict(n) for n in rows], "unread": unread, "email": notifications.email_channel()}


@router.get("/notifications/unread-count")
async def unread_count(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    n = (await db.execute(select(func.count()).select_from(MerchantNotification).where(
        MerchantNotification.business_id == ctx.business_id, MerchantNotification.status == "unread"))).scalar_one()
    return {"unread": n}


async def _notif(db, ctx, nid) -> MerchantNotification:
    n = (await db.execute(select(MerchantNotification).where(MerchantNotification.id == _uuid(nid, "Notification"),
                                                             MerchantNotification.business_id == ctx.business_id))).scalar_one_or_none()
    if n is None:
        raise not_found("Notification")
    return n


@router.post("/notifications/read-all")
async def read_all(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(MerchantNotification).where(MerchantNotification.business_id == ctx.business_id,
                                                                MerchantNotification.status == "unread"))).scalars().all()
    for n in rows:
        n.status, n.read_at = "read", now_utc()
    await db.commit()
    return {"updated": len(rows)}


@router.post("/notifications/{nid}/read")
async def read_one(nid: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    n = await _notif(db, ctx, nid)
    n.status, n.read_at = "read", now_utc()
    await db.commit()
    return notifications.notification_dict(n)


@router.post("/notifications/{nid}/dismiss")
async def dismiss_one(nid: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    n = await _notif(db, ctx, nid)
    n.status = "dismissed"
    await db.commit()
    return notifications.notification_dict(n)


# ================================================================ assistant
@router.post("/assistant/query")
async def assistant_query(body: QueryIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    if not await hit(f"assistant:{ctx.business_id}", 20):
        raise AppError(429, "rate_limited", "The assistant is limited to 20 questions per minute")
    if body.conversation_id:
        conv = (await db.execute(select(AIConversation).where(AIConversation.id == _uuid(body.conversation_id, "Conversation"),
                                                              AIConversation.business_id == ctx.business_id))).scalar_one_or_none()
        if conv is None:
            raise not_found("Conversation")
    else:
        conv = AIConversation(business_id=ctx.business_id, user_id=ctx.user.id if ctx.user else None, title=body.question[:120])
        db.add(conv)
        await db.flush()
    res = await assistant.answer(db, ctx.business, body.question, body.use_ai)
    db.add(AIConversationMessage(business_id=ctx.business_id, conversation_id=conv.id, role="user", content=body.question))
    msg = AIConversationMessage(business_id=ctx.business_id, conversation_id=conv.id, role="assistant", content=res["answer"],
                                intent=res["intent"], context={"facts": res["facts"], "referenced_product_ids": res["referenced_product_ids"],
                                                               "next_actions": res["next_actions"], "data_gaps": res["data_gaps"]},
                                source=res["source"], model_id=res["model_id"], grounding=res["grounding"])
    db.add(msg)
    conv.updated_at = now_utc()
    await db.commit()
    return {"conversation_id": str(conv.id), "message_id": str(msg.id), **res,
            "label": "AI-generated answer — verify before acting" if res["source"] == "groq" else "Answer generated from your data"}


@router.get("/assistant/conversations")
async def conversations(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AIConversation).where(AIConversation.business_id == ctx.business_id)
                             .order_by(AIConversation.updated_at.desc()).limit(50))).scalars().all()
    return {"items": [{"id": str(c.id), "title": c.title, "updated_at": aware(c.updated_at).isoformat()} for c in rows]}


@router.get("/assistant/conversations/{cid}")
async def conversation(cid: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(AIConversation).where(AIConversation.id == _uuid(cid, "Conversation"),
                                                       AIConversation.business_id == ctx.business_id))).scalar_one_or_none()
    if c is None:
        raise not_found("Conversation")
    msgs = (await db.execute(select(AIConversationMessage).where(AIConversationMessage.conversation_id == c.id)
                             .order_by(AIConversationMessage.created_at))).scalars().all()
    return {"id": str(c.id), "title": c.title, "messages": [
        {"id": str(m.id), "role": m.role, "content": m.content, "intent": m.intent, "context": m.context, "source": m.source,
         "model_id": m.model_id, "grounding": m.grounding, "created_at": aware(m.created_at).isoformat()} for m in msgs]}


async def _store_suggestion(db, ctx, p: Product, kind: str, sug: dict) -> uuid.UUID:
    for old in (await db.execute(select(SuggestedProductEdit).where(SuggestedProductEdit.product_id == p.id,
                                                                    SuggestedProductEdit.kind == kind,
                                                                    SuggestedProductEdit.status == "pending"))).scalars():
        old.status = "superseded"
    if kind == "description":
        original, proposed = {"description": p.description}, {"description": sug["proposed"]}
    else:
        original = {"attributes": [{"key": a.key, "label": a.label, "value": a.value, "unit": a.unit} for a in p.attributes]}
        proposed = {"attributes": sug["proposed_attributes"]}
    e = SuggestedProductEdit(business_id=ctx.business_id, product_id=p.id, kind=kind, original=original, proposed=proposed,
                             rationale="AI-generated suggestion" if sug["source"] == "groq" else "Generated from your existing product data",
                             missing_information=sug["missing_information"], source=sug["source"], model_id=sug.get("model_id"),
                             grounding=sug["grounding"], source_records={"product_id": str(p.id), "product_version": p.version,
                                                                         "facts": sug.get("facts")},
                             status="pending", product_version=p.version)
    db.add(e)
    await db.flush()
    return e.id


def _sug_dict(e: SuggestedProductEdit, name: str | None = None) -> dict:
    return {"id": str(e.id), "product_id": str(e.product_id), "product_name": name, "kind": e.kind, "original": e.original,
            "proposed": e.proposed, "rationale": e.rationale, "missing_information": e.missing_information, "source": e.source,
            "model_id": e.model_id, "grounding": e.grounding, "status": e.status, "created_at": aware(e.created_at).isoformat(),
            "decided_by": e.decided_by, "decided_at": aware(e.decided_at).isoformat() if e.decided_at else None,
            "label": "AI-generated — review before approving" if e.source == "groq" else "Generated from your data — review before approving"}


@router.post("/assistant/suggestions", status_code=201)
async def create_suggestion(body: SuggestIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    if not await hit(f"assistant:{ctx.business_id}", 20):
        raise AppError(429, "rate_limited", "The assistant is limited to 20 requests per minute")
    p = (await db.execute(select(Product).where(Product.id == _uuid(body.product_id, "Product"), *live_products(ctx.business_id)))).scalar_one_or_none()
    if p is None:
        raise not_found("Product")
    sug = (await assistant.suggest_description(db, ctx.business, p, body.use_ai) if body.kind == "description"
           else await assistant.suggest_attributes(db, ctx.business, p))
    if body.kind == "attributes":
        await engine.refresh(db, ctx.business)
    eid = await _store_suggestion(db, ctx, p, body.kind, sug)
    await db.commit()
    e = await db.get(SuggestedProductEdit, eid)
    return _sug_dict(e, p.name)


@router.get("/assistant/suggestions")
async def list_suggestions(status: str = Query("pending", pattern="^(pending|approved|rejected|superseded|all)$"),
                           ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    stmt = select(SuggestedProductEdit, Product.name).join(Product, Product.id == SuggestedProductEdit.product_id).where(
        SuggestedProductEdit.business_id == ctx.business_id)
    if status != "all":
        stmt = stmt.where(SuggestedProductEdit.status == status)
    rows = (await db.execute(stmt.order_by(SuggestedProductEdit.created_at.desc()).limit(100))).all()
    return {"items": [_sug_dict(e, n) for e, n in rows]}


async def _suggestion(db, ctx, sid) -> SuggestedProductEdit:
    e = (await db.execute(select(SuggestedProductEdit).where(SuggestedProductEdit.id == _uuid(sid, "Suggestion"),
                                                             SuggestedProductEdit.business_id == ctx.business_id))).scalar_one_or_none()
    if e is None:
        raise not_found("Suggestion")
    return e


@router.post("/assistant/suggestions/{sid}/approve")
async def approve_suggestion(sid: str, body: ApproveIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Applies the change through the normal product edit path (validation, merchant override, evidence, audit)."""
    from app.api.v1.products import update_product
    from app.schemas.api import AttributeIn
    from app.schemas.commerce import ProductUpdate

    e = await _suggestion(db, ctx, sid)
    if e.status != "pending":
        raise AppError(409, "not_pending", f"This suggestion is {e.status}")
    p = (await db.execute(select(Product).where(Product.id == e.product_id, *live_products(ctx.business_id)))).scalar_one_or_none()
    if p is None:
        raise not_found("Product")
    if e.kind == "description":
        if (p.description or None) != (e.original.get("description") or None):
            raise AppError(409, "product_changed", "The description changed since this suggestion was made; generate a new one "
                                                   "so your newer edit is not overwritten.")
        text = (body.description if body.description is not None else e.proposed["description"]).strip()
        if not text:
            raise AppError(422, "empty", "The description cannot be empty")
        upd = ProductUpdate(description=text)
        applied = {"description": text}
    else:
        current = {a.key for a in p.attributes}
        chosen = [a for a in e.proposed.get("attributes", []) if (body.attribute_keys is None or a["key"] in body.attribute_keys)
                  and a["key"] not in current]
        if not chosen:
            raise AppError(422, "nothing_selected", "No new attributes selected")
        attrs = [AttributeIn(key=a.key, label=a.label, value=a.value, unit=a.unit) for a in p.attributes]
        attrs += [AttributeIn(key=a["key"], label=a["label"], value=a["value"], unit=a.get("unit")) for a in chosen]
        upd = ProductUpdate(attributes=attrs)
        applied = {"attributes": chosen}
    await update_product(str(p.id), upd, ctx, db)
    e = await _suggestion(db, ctx, sid)
    e.status, e.decided_by, e.decided_at = "approved", ctx.user.email if ctx.user else None, now_utc()
    if body.description is not None and e.kind == "description":
        e.proposed = {**e.proposed, "merchant_edited": body.description}
    record(db, ctx.business_id, actor_from_ctx(ctx), "intelligence.suggestion_approved", "product", e.product_id,
           f"Approved {'AI-generated' if e.source == 'groq' else 'assistant'} {e.kind} suggestion",
           {"suggestion_id": str(e.id), "source": e.source, "model": e.model_id, "applied": applied})
    await db.commit()
    return _sug_dict(e)


@router.post("/assistant/suggestions/{sid}/reject")
async def reject_suggestion(sid: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    e = await _suggestion(db, ctx, sid)
    if e.status != "pending":
        raise AppError(409, "not_pending", f"This suggestion is {e.status}")
    e.status, e.decided_by, e.decided_at = "rejected", ctx.user.email if ctx.user else None, now_utc()
    await db.commit()
    return _sug_dict(e)
