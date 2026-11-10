"""Merchant reports (PDF, CSV, JSON) built from the same deterministic services as the dashboards.

Every report states its period, merchant, generation time, metric definitions, data coverage and missing-data
warnings. Unavailable metrics are reported as ``null`` with a reason — never as zero.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (DistributionEvent, IntelligenceRecommendation, IntelligenceReport, InventoryTransaction, PriceHistory,
                        SyncConflict)
from app.services.commerce.money import aware, now_utc
from app.services.intelligence import anomalies, discovery, engine, pdf
from app.services.intelligence.quality import DEFAULT_WEIGHTS, DIMENSIONS, LEVELS

KINDS = {
    "catalog_quality": "Catalog quality report",
    "missing_attributes": "Missing attribute report",
    "product_completeness": "Product completeness report",
    "distribution_activity": "Distribution activity report",
    "search_performance": "Search performance report",
    "freshness": "Product information freshness report",
    "pricing_history": "Pricing history report",
    "inventory_health": "Inventory health report",
    "sync_conflicts": "Synchronization conflict report",
    "recommendations": "AI recommendation report",
}
FORMATS = {"pdf": "application/pdf", "csv": "text/csv; charset=utf-8", "json": "application/json"}


async def _coverage(db, business_id, start: datetime) -> dict:
    first_event = (await db.execute(select(func.min(DistributionEvent.created_at)).where(
        DistributionEvent.business_id == business_id))).scalar_one()
    first_price = (await db.execute(select(func.min(PriceHistory.created_at)).where(PriceHistory.business_id == business_id))).scalar_one()
    hist = await engine.history(db, business_id, 3650)
    warnings = []
    if first_event is None:
        warnings.append("No agent/distribution events have ever been recorded: activity metrics are unavailable, not zero.")
    elif aware(first_event) > start:
        warnings.append(f"Distribution events start {aware(first_event).date()}, after the start of the period.")
    if not hist:
        warnings.append("No quality snapshots yet: trends are unavailable.")
    elif hist[0]["day"] > start.date().isoformat():
        warnings.append(f"Quality history starts {hist[0]['day']}, after the start of the period.")
    return {"events_since": aware(first_event).isoformat() if first_event else None,
            "price_history_since": aware(first_price).isoformat() if first_price else None,
            "quality_history_since": hist[0]["day"] if hist else None, "warnings": warnings,
            "activity_available": first_event is not None}


async def build(db: AsyncSession, business, kind: str, days: int) -> dict:
    """Returns {"definitions", "sections": [{"title", "columns", "rows", "note"}], "summary"}."""
    end = now_utc()
    start = end - timedelta(days=days)
    coverage = await _coverage(db, business.id, start)
    await engine.refresh(db, business)
    rows = await engine.results(db, business.id)
    summ = engine.summarize(rows)
    defs: dict[str, str] = {}
    sections: list[dict] = []
    summary: dict = {}
    if kind in ("catalog_quality", "product_completeness", "missing_attributes", "freshness"):
        defs.update({"score": "Weighted average of applicable quality dimensions (0-100); measures completeness, validity, "
                              "freshness and provenance, not factual accuracy.",
                     "levels": ", ".join(f"{n} >= {low}" for n, low in LEVELS),
                     "weights": ", ".join(f"{k} {v}" for k, v in DEFAULT_WEIGHTS.items())})
        summary = {"products": summ["products"], "average_score": summ["average_score"], "levels": summ["levels"],
                   "attention": summ["attention"], "stale": summ["stale"], "distribution_ready": summ["distribution_ready"]}
    if kind == "catalog_quality":
        sections.append({"title": "Dimensions", "columns": ["dimension", "average", "products"],
                         "rows": [[DIMENSIONS[k], v["average"], v["products"]] for k, v in summ["dimensions"].items()]})
        sections.append({"title": "Categories", "columns": ["category", "products", "average"],
                         "rows": [[c["category"], c["products"], c["average"]] for c in summ["categories"]]})
        hist = await engine.history(db, business.id, days)
        sections.append({"title": "Trend", "columns": ["day", "average_score", "products", "attention"],
                         "rows": [[h["day"], h["average_score"], h["products"], h["attention_products"]] for h in hist],
                         "note": None if hist else "No snapshots in this period."})
    elif kind == "product_completeness":
        sections.append({"title": "Products", "columns": ["product", "sku", "category", "score", "level", "missing_required", "missing_optional"],
                         "rows": [[p.name, p.sku, p.category, r.score, r.level, "; ".join(r.missing_required), "; ".join(r.missing_optional)]
                                  for r, p in sorted(rows, key=lambda x: x[0].score)]})
    elif kind == "missing_attributes":
        sections.append({"title": "Missing required fields", "columns": ["field", "products"],
                         "rows": [[m["field"], m["products"]] for m in summ["missing_required"]]})
        sections.append({"title": "Products", "columns": ["product", "category", "missing"],
                         "rows": [[p.name, p.category, "; ".join(f for f in r.missing_required if f.startswith("attribute:"))]
                                  for r, p in rows if any(f.startswith("attribute:") for f in r.missing_required)]})
    elif kind == "freshness":
        sections.append({"title": "Stale products", "columns": ["product", "stale", "details"],
                         "rows": [[p.name, ", ".join(r.stale), "; ".join(r.dimensions.get("freshness", {}).get("issues", []))]
                                  for r, p in rows if r.stale]})
    elif kind in ("distribution_activity", "search_performance"):
        defs["requests"] = discovery.DISCLAIMER
        if not coverage["activity_available"]:
            summary = {"searches": None, "requests": None, "reason": "No distribution events recorded"}
        else:
            ins = await discovery.search_insights(db, business.id, days)
            interest = await discovery.product_interest(db, business.id, days, 100)
            summary = {"searches": ins["searches"], "products_with_requests": len(interest["products"]),
                       "zero_result_queries": len(ins["zero_result_queries"])}
            if kind == "search_performance":
                sections += [{"title": "Top queries", "columns": ["query", "count"], "rows": [[q["query"], q["count"]] for q in ins["top_queries"]]},
                             {"title": "Zero-result queries", "columns": ["query", "count", "last_seen"],
                              "rows": [[z["query"] + "".join(f" [{a}]" for a in z["attribute_filters"]), z["count"], z["last_seen"]]
                                       for z in ins["zero_result_queries"]]},
                             {"title": "Filters used", "columns": ["filter", "count"], "rows": [[f["filter"], f["count"]] for f in ins["filters_used"]]}]
            else:
                trend = await discovery.activity_trend(db, business.id, days)
                sections += [{"title": "Daily activity", "columns": ["day", "searches", "product_requests", "mcp_calls", "feed_downloads"],
                              "rows": [[t["day"], t.get("searches", 0), t.get("product_requests", 0), t.get("mcp_calls", 0),
                                        t.get("feed_downloads", 0)] for t in trend]},
                             {"title": "Most requested products", "columns": ["product", "detail", "pricing", "availability", "compare", "total"],
                              "rows": [[x["name"], x["product_detail"], x["pricing"], x["availability"], x["compare"], x["total"]]
                                       for x in interest["products"]]}]
    elif kind == "pricing_history":
        pi = await anomalies.pricing_insights(db, business.id, days)
        defs["changes"] = "Entries in the immutable price history (manual, bulk, scheduled, website sync, integration)."
        summary = {"changes": len(pi["recent_changes"]), "scheduled": len(pi["scheduled_changes"]), "invalid": len(pi["invalid_prices"])}
        sections.append({"title": "Price changes", "columns": ["product", "type", "old", "new", "currency", "change", "at"],
                         "rows": [[c["product"]["name"], c["price_type"], c["old"], c["new"], c["currency"], c["change_type"], c["at"]]
                                  for c in pi["recent_changes"]]})
    elif kind == "inventory_health":
        ii = await anomalies.inventory_insights(db, business.id, days)
        defs["forecast"] = "Forecasting needs real order data; manual adjustments are not demand."
        summary = {"tracked": ii["tracked"], "low_stock": len(ii["low_stock"]), "out_of_stock": len(ii["out_of_stock"]),
                   "stale_stock": len(ii["stale_stock"]), "forecast": None, "forecast_reason": ii["forecast"]["reason"]}
        sections.append({"title": "Low / out of stock", "columns": ["product", "sku", "status"],
                         "rows": [[x["name"], x["sku"], x["status"]] for x in ii["low_stock"] + ii["out_of_stock"]]})
        tx = (await db.execute(select(func.count()).select_from(InventoryTransaction).where(
            InventoryTransaction.business_id == business.id, InventoryTransaction.created_at >= start))).scalar_one()
        summary["transactions"] = tx
    elif kind == "sync_conflicts":
        cs = (await db.execute(select(SyncConflict).where(SyncConflict.business_id == business.id,
                                                          SyncConflict.detected_at >= start))).scalars().all()
        summary = {"conflicts": len(cs), "open": sum(1 for c in cs if c.status == "open")}
        sections.append({"title": "Conflicts", "columns": ["product_id", "field", "website", "agentready", "status", "resolution", "detected"],
                         "rows": [[str(c.product_id), c.field, json.dumps(c.website_value), json.dumps(c.agentready_value),
                                   c.status, c.resolution, aware(c.detected_at).isoformat()] for c in cs]})
    elif kind == "recommendations":
        recs = (await db.execute(select(IntelligenceRecommendation).where(IntelligenceRecommendation.business_id == business.id))).scalars().all()
        defs["priority"] = "Rule-based: see priority_rule on each row."
        summary = {"open": sum(1 for r in recs if r.status == "open"), "dismissed": sum(1 for r in recs if r.status == "dismissed"),
                   "applied": sum(1 for r in recs if r.status == "applied")}
        sections.append({"title": "Recommendations", "columns": ["priority", "title", "status", "products", "source", "suggested_action"],
                         "rows": [[r.priority, r.title, r.status, len(r.product_ids or []), r.source, r.suggested_action] for r in recs]})
    return {"definitions": defs, "sections": sections, "summary": summary, "coverage": coverage,
            "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days}}


def render(fmt: str, title: str, merchant: str, data: dict) -> bytes:
    meta = {"report": title, "merchant": merchant, "generated_at": now_utc().isoformat(), **data}
    if fmt == "json":
        return json.dumps(meta, ensure_ascii=False, indent=2, default=str).encode()
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        safe = lambda v: "" if v is None else (("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v)  # noqa: E731
        w.writerow(["report", title])
        w.writerow(["merchant", merchant])
        w.writerow(["period", data["period"]["start"], data["period"]["end"]])
        w.writerow(["generated_at", meta["generated_at"]])
        for k, v in data["summary"].items():
            w.writerow(["summary", k, "unavailable" if v is None else json.dumps(v) if isinstance(v, dict) else v])
        for wmsg in data["coverage"]["warnings"]:
            w.writerow(["warning", wmsg])
        for k, v in data["definitions"].items():
            w.writerow(["definition", k, v])
        for s in data["sections"]:
            w.writerow([])
            w.writerow(["section", s["title"]])
            w.writerow(s["columns"])
            for r in s["rows"]:
                w.writerow([safe(x) for x in r])
        return buf.getvalue().encode("utf-8")
    blocks = [("p", f"Merchant: {merchant}"), ("p", f"Period: {data['period']['start'][:10]} to {data['period']['end'][:10]}"),
              ("small", f"Generated {meta['generated_at']}")]
    if data["coverage"]["warnings"]:
        blocks.append(("h2", "Data coverage warnings"))
        blocks += [("p", f"- {w}") for w in data["coverage"]["warnings"]]
    blocks.append(("h1", "Summary"))
    blocks += [("row", f"{k}: {'unavailable' if v is None else v}") for k, v in data["summary"].items()]
    for s in data["sections"]:
        blocks.append(("h1", s["title"]))
        blocks.append(("small", " | ".join(s["columns"])))
        blocks += [("row", " | ".join("" if x is None else str(x) for x in r)) for r in s["rows"][:400]]
        if not s["rows"]:
            blocks.append(("p", s.get("note") or "No rows."))
    if data["definitions"]:
        blocks.append(("h1", "Definitions"))
        blocks += [("small", f"{k}: {v}") for k, v in data["definitions"].items()]
    return pdf.render(f"{title}", blocks)


async def generate(db: AsyncSession, business, kind: str, fmt: str, days: int, user_label: str | None) -> IntelligenceReport:
    title = KINDS[kind]
    data = await build(db, business, kind, days)
    content = render(fmt, title, business.name, data)
    rep = IntelligenceReport(business_id=business.id, kind=kind, format=fmt, title=title,
                             period_start=datetime.fromisoformat(data["period"]["start"]),
                             period_end=datetime.fromisoformat(data["period"]["end"]), status="completed", data=content,
                             content_type=FORMATS[fmt], filename=f"{kind}-{now_utc().date()}.{fmt}", size_bytes=len(content),
                             coverage=data["coverage"], created_by=user_label)
    db.add(rep)
    await db.flush()
    return rep


def report_dict(r: IntelligenceReport) -> dict:
    return {"id": str(r.id), "kind": r.kind, "title": r.title, "format": r.format, "status": r.status,
            "period_start": aware(r.period_start).isoformat() if r.period_start else None,
            "period_end": aware(r.period_end).isoformat() if r.period_end else None, "filename": r.filename,
            "size_bytes": r.size_bytes, "coverage": r.coverage, "error": r.error, "created_by": r.created_by,
            "created_at": aware(r.created_at).isoformat()}
