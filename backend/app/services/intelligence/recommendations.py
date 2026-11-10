"""Deterministic catalog recommendations with transparent priority rules.

Each recommendation is derived from stored quality results, anomalies and observed agent activity. Its identity
(``fingerprint``) is stable, so a dismissed recommendation stays dismissed while it still applies, and recommendations
whose condition no longer holds are marked resolved. Nothing here changes product data.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, IntelligenceRecommendation, ProductAnomaly
from app.services.commerce.money import aware, now_utc
from app.services.intelligence import discovery, engine

PRIORITY_RULES = {
    "critical": "Blocks agents from finding products they are actively asking for (zero-result searches caused by "
                "missing data, or distribution blockers on products with observed agent interest).",
    "high": "Affects products with observed agent interest, affects at least 25% of the catalog, or concerns required "
            "commercial data (price, currency, duplicate identifiers).",
    "medium": "Required catalog information is missing or invalid on at least one product.",
    "low": "Optional improvements (identifiers, freshness, extra images) that make data more useful.",
}
ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _priority(base: str, affected: set, interest: set, catalog: int, blocks_discovery: bool = False) -> tuple[str, str]:
    hit = affected & interest
    if blocks_discovery and hit:
        return "critical", PRIORITY_RULES["critical"]
    if blocks_discovery:
        return "critical", PRIORITY_RULES["critical"]
    if hit or (catalog and len(affected) / catalog >= 0.25 and base != "low") or base == "high":
        return "high", PRIORITY_RULES["high"] + (f" ({len(hit)} affected product(s) received agent requests)" if hit else "")
    return base, PRIORITY_RULES[base]


async def generate(db: AsyncSession, business, days: int = 30) -> list[dict]:
    rows = await engine.results(db, business.id)
    catalog = len(rows)
    interest = {r["product_id"] for r in (await discovery.product_interest(db, business.id, days, 1000))["products"]}
    zero = await discovery.zero_result_opportunities(db, business.id, days)
    by_id = {str(p.id): (r, p) for r, p in rows}
    recs: list[dict] = []

    def add(fp, kind, title, desc, reason, products, base, benefit, action_text, evidence=None, blocks=False, action=None):
        if not products:
            return
        ids = sorted({str(x) for x in products})
        pr, rule = _priority(base, set(ids), interest, catalog, blocks)
        recs.append({"fingerprint": fp, "kind": kind, "title": title, "description": desc, "reason": reason,
                     "product_ids": ids, "priority": pr, "priority_rule": rule, "expected_benefit": benefit,
                     "suggested_action": action_text, "evidence": {**(evidence or {}), "affected_products": len(ids),
                                                                   "with_agent_interest": len(set(ids) & interest)},
                     "requires_approval": True, "action": action})

    # 1. missing attributes, per category and attribute (+ zero-result evidence)
    missing: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r, p in rows:
        for f in r.missing_required or []:
            if f.startswith("attribute:"):
                missing[(p.category or "Uncategorised", f.split(":", 1)[1])].append(str(p.id))
    zero_by_attr: dict[str, list[dict]] = defaultdict(list)
    for z in zero:
        if z["kind"] == "missing_attribute":
            for a in z["requested_attributes"]:
                zero_by_attr[a].append(z)
    for (cat, attr), ids in missing.items():
        zs = zero_by_attr.get(attr, [])
        add(f"missing_attribute:{cat}:{attr}", "missing_attributes", f"Complete {attr} specifications in {cat}",
            f"{len(ids)} product(s) in {cat} have no {attr} value, which is required for this category.",
            "Required attribute definitions for this category are not satisfied."
            + (f" External agents searched with a {attr} filter {sum(z['count'] for z in zs)} time(s) and got no results." if zs else ""),
            ids, "medium", "Agents can filter and compare these products by this specification.",
            "Add the values from verified sources (datasheets, packaging). Do not estimate technical values.",
            {"category": cat, "attribute": attr, "zero_result_queries": [z["query"] for z in zs][:5]}, blocks=bool(zs))
    for attr, zs in zero_by_attr.items():
        ids = {a["id"] for z in zs for a in z["affected"]}
        if not any(k[1] == attr for k in missing):
            add(f"zero_result_attribute:{attr}", "search_interest_incomplete", f"Add {attr} to products agents could not find",
                f"Agents searched with a {attr} filter and got no results, while {len(ids)} matching product(s) have no {attr}.",
                "Zero-result searches matched your products by keywords, but the requested attribute is missing.", ids, "medium",
                "These products can appear in attribute-filtered agent searches.", f"Add verified {attr} values.",
                {"queries": [{"query": z["query"], "filters": z["attribute_filters"], "count": z["count"]} for z in zs][:5]},
                blocks=True)

    def ids_where(pred):
        return [str(p.id) for r, p in rows if pred(r, p)]

    # 2. descriptions
    add("descriptions", "incomplete_descriptions", "Improve missing or very short descriptions",
        "Products without an informative description are hard for agents to match and explain.",
        "Description quality is below 50/100 (missing, under 60 characters or only repeating the name).",
        ids_where(lambda r, p: (r.dimensions.get("description", {}).get("score") or 0) < 50), "medium",
        "Better matching for keyword searches and clearer answers from agents.",
        "Write or restructure descriptions; the assistant can draft one from existing facts for your review.",
        action="draft_descriptions")
    # 3. images
    add("images", "missing_images", "Add product images", "Products without images are less useful in agent answers and feeds.",
        "No image is stored for these products.", ids_where(lambda r, p: "images" in (r.missing_required or [])), "medium",
        "Image-capable agents and platform feeds (which require an image) can include these products.", "Upload or link product images.")
    # 4. identifiers
    add("identifiers", "missing_identifiers", "Add GTIN/barcodes or SKUs",
        "Products without identifiers cannot be matched exactly by SKU/GTIN searches.", "Neither GTIN nor SKU is set.",
        ids_where(lambda r, p: not p.gtin and not p.sku), "low", "Exact identifier searches find these products.",
        "Add identifiers you actually have; never invent a GTIN.")
    # 5. inconsistent specifications
    add("invalid_specs", "inconsistent_specifications", "Fix invalid or inconsistent specifications",
        "Some values do not match their attribute definition (type, allowed values or unit), or variants use different option names.",
        "Attribute definitions or variant options are violated.",
        ids_where(lambda r, p: any(f.startswith("attribute:") or f == "variant_options" for f in r.invalid or [])), "medium",
        "Filters and comparisons work on consistent values.", "Correct the values flagged on each product's quality page.")
    # 6/7. freshness
    add("stale_prices", "stale_prices", "Confirm prices that have not changed for a long time",
        "These prices have not been changed or confirmed within your freshness threshold.", "No price history entry within the threshold.",
        ids_where(lambda r, p: "price" in (r.stale or [])), "low", "Agents serve prices you have recently confirmed.",
        "Review the prices; saving a price records a new confirmation.")
    add("stale_stock", "stale_availability", "Recount stock that has not changed for a long time",
        "Tracked stock has not been counted or adjusted within your threshold.", "No inventory transaction within the threshold.",
        ids_where(lambda r, p: "stock" in (r.stale or [])), "low", "Availability served to agents stays trustworthy.",
        "Do a stock count and record it.")
    # 8. distribution blockers
    blocked = ids_where(lambda r, p: bool(r.blockers) and p.approval_status == "approved")
    add("distribution_blockers", "distribution_blocked", "Resolve distribution blockers on approved products",
        "Approved products that agents cannot see.", "Distribution status lists blocking reasons (see each product).",
        blocked, "medium", "These products become visible to agents.",
        "Review each product's blockers (publication, lifecycle, channel or category settings).",
        blocks=bool(set(blocked) & interest))
    # 9. interest but incomplete
    add("interest_incomplete", "search_interest_incomplete", "Complete products that agents are requesting",
        "These products receive agent requests but their quality score is below 70.",
        "Observed agent requests (detail, pricing, availability, comparison) + quality below 'good'.",
        ids_where(lambda r, p: str(p.id) in interest and r.score < 70), "high",
        "Agents asking about these products get more complete answers.", "Fix the issues listed on each product's quality page.")
    # 10. duplicates
    dups = (await db.execute(select(ProductAnomaly).where(ProductAnomaly.business_id == business.id,
                                                         ProductAnomaly.rule == "duplicate_identifier",
                                                         ProductAnomaly.status == "open"))).scalars().all()
    for a in dups:
        add(f"duplicate:{a.fingerprint}", "possible_duplicates", a.title, "Several products share the same identifier.",
            "Duplicate identifiers detected by anomaly rules.", [x["id"] for x in a.evidence.get("products", [])], "high",
            "Agents and marketplaces can tell these products apart.", "Merge duplicates or correct the wrong identifier.",
            {"anomaly_id": str(a.id), **a.evidence})
    # 11. category problems (with an automated, confirmed action when an existing category matches)
    cats = {c.path.lower(): c for c in (await db.execute(select(Category).where(Category.business_id == business.id,
                                                                                Category.archived_at.is_(None)))).scalars()}
    mappable = ids_where(lambda r, p: p.category_id is None and p.category and p.category.lower() in cats)
    add("category_mappable", "category_classification", "Link products to your managed categories",
        "These products carry a category text that exactly matches one of your categories but are not linked to it.",
        "Category text equals an existing category path.", mappable, "low",
        "Category-specific requirements and category feeds apply to these products.",
        "Apply to link each product to the matching category (you will be asked to confirm).", action="assign_category")
    add("category_missing", "category_classification", "Categorise uncategorised products", "Products without any category.",
        "No category text or managed category.", ids_where(lambda r, p: not p.category and p.category_id is None), "medium",
        "Category browsing and category-specific scoring apply.", "Assign categories in the catalog (bulk action available).")
    # 12. variants
    add("variants", "missing_variant_information", "Complete variant information",
        "Some variants have no SKU or no option values, or use inconsistent option names.", "Variant completeness below 100.",
        ids_where(lambda r, p: (r.dimensions.get("variants", {}).get("score") is not None
                                and r.dimensions["variants"]["score"] < 100)), "medium",
        "Agents can match size/colour requests to the right variant.", "Add SKUs and options to each variant.")
    for rec in recs:
        rec["products"] = [{"id": i, "name": by_id[i][1].name if i in by_id else None} for i in rec["product_ids"][:50]]
    return recs


async def refresh(db: AsyncSession, business) -> dict:
    found = {r["fingerprint"]: r for r in await generate(db, business)}
    existing = {r.fingerprint: r for r in (await db.execute(select(IntelligenceRecommendation).where(
        IntelligenceRecommendation.business_id == business.id))).scalars()}
    now = now_utc()
    new = 0
    for fp, r in found.items():
        row = existing.get(fp)
        fields = {k: r[k] for k in ("kind", "title", "description", "reason", "product_ids", "priority", "priority_rule",
                                    "expected_benefit", "suggested_action", "requires_approval", "action")}
        fields["evidence"] = {**r["evidence"], "products": r["products"]}
        if row is None:
            db.add(IntelligenceRecommendation(business_id=business.id, fingerprint=fp, status="open", source="rules",
                                              last_seen_at=now, **fields))
            new += 1
        else:
            grew = len(set(r["product_ids"]) - set(row.product_ids or [])) > 0
            for k, v in fields.items():
                setattr(row, k, v)
            row.last_seen_at = now
            if row.status == "resolved" or (row.status == "applied" and grew):
                row.status, row.resolved_at = "open", None
    for fp, row in existing.items():
        if fp not in found and row.status in ("open", "applied"):
            row.status, row.resolved_at = "resolved", now
    await db.commit()
    return {"recommendations": len(found), "new": new}


def rec_dict(r: IntelligenceRecommendation) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(r.id), "kind": r.kind, "title": r.title, "description": r.description, "reason": r.reason,
            "evidence": r.evidence, "product_ids": r.product_ids, "priority": r.priority, "priority_rule": r.priority_rule,
            "expected_benefit": r.expected_benefit, "suggested_action": r.suggested_action,
            "requires_approval": r.requires_approval, "action": r.action, "status": r.status, "source": r.source,
            "model_id": r.model_id, "created_at": iso(r.created_at), "last_seen_at": iso(r.last_seen_at),
            "dismissed_at": iso(r.dismissed_at), "applied_at": iso(r.applied_at), "decided_by": r.decided_by}


__all__ = ["generate", "refresh", "rec_dict", "PRIORITY_RULES", "ORDER", "uuid"]
