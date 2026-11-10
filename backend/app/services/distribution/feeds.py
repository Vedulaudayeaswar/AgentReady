"""Feed distribution: configurations, versioned snapshots, validation, public token URLs.

All formats are rendered from the canonical public product representation (``public.public_product``) of
products that are distributable through the *feeds* channel — never from unapproved or internal data.

Profiles:
* ``agentready`` — the canonical representation (json, jsonl, csv, jsonld, xml); all feed types.
* ``openai``     — OpenAI's Product Feed spec field names (jsonl, csv). Rows missing a required field are
                   excluded and listed in the validation report rather than filled with invented values.
* ``google``     — Google Merchant Center product data attributes (xml RSS 2.0 with the g: namespace, csv).

Incremental snapshots diff content hashes against the previous completed snapshot's manifest, so a consumer gets
exactly the products that changed plus tombstones for products that left the feed — no duplicates.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import uuid
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import generate_opaque_token, hash_opaque_token
from app.models import Business, DistributionProfile, FeedConfiguration, FeedSnapshot, FeedToken, Product
from app.services.commerce.audit import Actor, record
from app.services.commerce.money import aware, now_utc
from app.services.distribution import analytics
from app.services.distribution.profile import channel_open, merchant_scope
from app.services.distribution.public import SCHEMA, eligibility_clause, public_product, removal_reason
from app.services.normalization.normalize import gtin_is_valid

log = logging.getLogger("agentready.feeds")
FEED_SCHEMA = "agentready-feed/1.0"
PROFILES = {"agentready": ("json", "jsonl", "csv", "jsonld", "xml"), "openai": ("jsonl", "csv"), "google": ("xml", "csv")}
FEED_TYPES = ("full", "incremental", "availability", "pricing", "category", "variant")
PROFILE_TYPES = {"agentready": FEED_TYPES, "openai": ("full", "category"), "google": ("full", "category")}
INCREMENTAL_FORMATS = ("json", "jsonl")
SCHEDULES = {"manual": None, "hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7)}
CONTENT_TYPES = {"json": "application/json", "jsonl": "application/x-ndjson", "csv": "text/csv; charset=utf-8",
                 "jsonld": "application/ld+json", "xml": "application/xml"}
MAX_FEED_BYTES = 50_000_000
INLINE_REGENERATE_LIMIT = 1000  # public URLs regenerate expired feeds synchronously up to this many products
CHUNK = 200
_inline_tasks: set[asyncio.Task] = set()


class FeedError(ValueError):
    pass


def validate_config(profile: str, fmt: str, feed_type: str, category_id) -> None:
    if profile not in PROFILES:
        raise FeedError(f"Unknown profile '{profile}'")
    if fmt not in PROFILES[profile]:
        raise FeedError(f"The {profile} profile supports {', '.join(PROFILES[profile])}")
    if feed_type not in PROFILE_TYPES[profile]:
        raise FeedError(f"The {profile} profile supports feed types: {', '.join(PROFILE_TYPES[profile])}")
    if feed_type == "incremental" and fmt not in INCREMENTAL_FORMATS:
        raise FeedError("Incremental feeds are available as json or jsonl (they contain removal tombstones)")
    if feed_type == "category" and not category_id:
        raise FeedError("A category feed needs a category")


def next_run(schedule: str, after: datetime | None = None) -> datetime | None:
    step = SCHEDULES.get(schedule)
    return (after or now_utc()) + step if step else None


def content_hash(pub: dict) -> str:
    # 'availability.as_of' is the request time, not product content: hash the status only
    stable = {k: v for k, v in pub.items() if k != "availability"}
    stable["availability"] = (pub.get("availability") or {}).get("status")
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ record builders
def _money(amount: str | None, cur: str | None) -> str | None:
    return f"{amount} {cur}" if amount and cur else None


def _avail_openai(status: str | None) -> str:
    return {"available": "in_stock", "out_of_stock": "out_of_stock", "preorder": "pre_order", "backorder": "backorder",
            "discontinued": "out_of_stock"}.get(status or "", "unknown")


def _avail_google(status: str | None) -> str | None:
    return {"available": "in_stock", "out_of_stock": "out_of_stock", "preorder": "preorder", "backorder": "backorder",
            "discontinued": "out_of_stock"}.get(status or "")


def _attr(pub: dict, *keys) -> str | None:
    for a in pub["attributes"]:
        if a["key"].lower() in keys:
            return f"{a['value']} {a['unit']}".strip() if a["unit"] else a["value"]
    return None


def _rows_for_variants(pub: dict) -> list[tuple[dict | None, dict]]:
    """(variant or None, pub) rows: one per live variant, or one for the product."""
    return [(v, pub) for v in pub["variants"]] or [(None, pub)]


def openai_rows(pub: dict, prof: DistributionProfile, seller: str) -> tuple[list[dict], list[str], list[str]]:
    errors, warnings, rows = [], [], []
    path = (pub.get("category") or {}).get("path")
    for v, p in _rows_for_variants(pub):
        price = (v or {}).get("price") or p.get("price")
        avail = ((v or {}).get("availability") or p.get("availability") or {}).get("status")
        images = ([v["image"]] if v and v.get("image") else []) + (p["images"] or [])
        title = p["name"] or ""
        if v and v.get("name"):
            title = f"{title} - {v['name']}"
        if len(title) > 150:
            warnings.append(f"{p['id']}: title shortened to 150 characters")
            title = title[:149] + "…"
        desc = p["description"] or ""
        if len(desc) > 5000:
            warnings.append(f"{p['id']}: description shortened to 5000 characters")
            desc = desc[:4999] + "…"
        row = {
            "item_id": v["id"] if v else p["id"], "title": title, "description": desc, "url": p["url"],
            "brand": p["brand"], "seller_name": seller, "image_url": images[0] if images else None,
            "availability": _avail_openai(avail),
            "price": _money(price["regular"], price["currency"]) if price else None,
            "is_eligible_search": True, "is_eligible_checkout": False,
        }
        if price and price.get("on_sale") and price.get("sale"):
            row["sale_price"] = _money(price["sale"], price["currency"])
        if len(images) > 1:
            row["additional_image_urls"] = images[1:11]
        gtin = p["identifiers"]["gtin"]
        if gtin:
            if gtin_is_valid(gtin):
                row["gtin"] = gtin
            else:
                warnings.append(f"{p['id']}: GTIN {gtin} has an invalid check digit and was omitted")
        if p["identifiers"]["mpn"]:
            row["mpn"] = p["identifiers"]["mpn"]
        if v:
            row.update(group_id=p["id"], listing_has_variations=True, variant_dict=v["options"] or {})
            opts = {k.lower(): str(val) for k, val in (v["options"] or {}).items()}
            if opts.get("color") or opts.get("colour"):
                row["color"] = opts.get("color") or opts.get("colour")
            if opts.get("size"):
                row["size"] = opts["size"]
        if path:
            row["product_category"] = path.replace(" / ", " > ")
        if (p.get("specifications") or {}).get("material"):
            row["material"] = p["specifications"]["material"]
        if prof.public_url:
            row["seller_url"] = prof.public_url
        if prof.return_policy_url:
            row["return_policy"] = prof.return_policy_url
        if prof.privacy_policy_url:
            row["seller_privacy_policy"] = prof.privacy_policy_url
        if prof.terms_url:
            row["seller_tos"] = prof.terms_url
        if prof.country:
            row["store_country"] = prof.country
        missing = [f for f in ("item_id", "title", "description", "url", "brand", "seller_name", "image_url", "price")
                   if not row.get(f)]
        if missing:
            errors.append(f"{p['id']}{' variant ' + v['id'] if v else ''}: missing required {', '.join(missing)}")
            continue
        rows.append(row)
    return rows, errors, warnings


def google_rows(pub: dict, prof: DistributionProfile) -> tuple[list[dict], list[str], list[str]]:
    errors, warnings, rows = [], [], []
    for v, p in _rows_for_variants(pub):
        price = (v or {}).get("price") or p.get("price")
        avail = _avail_google(((v or {}).get("availability") or p.get("availability") or {}).get("status"))
        images = ([v["image"]] if v and v.get("image") else []) + (p["images"] or [])
        row = {"id": v["id"] if v else p["id"], "title": (p["name"] or "")[:150], "description": (p["description"] or "")[:5000],
               "link": p["url"], "image_link": images[0] if images else None, "availability": avail,
               "price": _money(price["regular"], price["currency"]) if price else None, "brand": p["brand"]}
        if price and price.get("on_sale") and price.get("sale"):
            row["sale_price"] = _money(price["sale"], price["currency"])
            if price.get("sale_starts_at") and price.get("sale_ends_at"):
                row["sale_price_effective_date"] = f"{price['sale_starts_at']}/{price['sale_ends_at']}"
        if len(images) > 1:
            row["additional_image_link"] = ",".join(images[1:11])
        gtin = p["identifiers"]["gtin"]
        if gtin and gtin_is_valid(gtin):
            row["gtin"] = gtin
        elif gtin:
            warnings.append(f"{p['id']}: GTIN {gtin} has an invalid check digit and was omitted")
        if p["identifiers"]["mpn"]:
            row["mpn"] = p["identifiers"]["mpn"]
        if not row.get("gtin") and not row.get("mpn"):
            row["identifier_exists"] = "no"
        if v:
            row["item_group_id"] = p["id"]
            opts = {k.lower(): str(val) for k, val in (v["options"] or {}).items()}
            for k in ("color", "size", "material"):
                if opts.get(k):
                    row[k] = opts[k]
        if (p.get("category") or {}).get("path"):
            row["product_type"] = p["category"]["path"].replace(" / ", " > ")
        missing = [f for f in ("id", "title", "description", "link", "image_link", "availability", "price") if not row.get(f)]
        if missing:
            errors.append(f"{p['id']}{' variant ' + v['id'] if v else ''}: missing required {', '.join(missing)}")
            continue
        if not row.get("brand"):
            warnings.append(f"{p['id']}: no brand (required by Google for most new products)")
            row.pop("brand")
        rows.append(row)
    return rows, errors, warnings


def compact(pub: dict, feed_type: str) -> list[dict]:
    if feed_type == "availability":
        return [{"id": pub["id"], "sku": pub["identifiers"]["sku"], "availability": pub["availability"],
                 "variants": [{"id": v["id"], "sku": v["sku"], "availability": v["availability"]} for v in pub["variants"]],
                 "updated_at": pub["updated_at"]}]
    if feed_type == "pricing":
        return [{"id": pub["id"], "sku": pub["identifiers"]["sku"], "price": pub["price"],
                 "variants": [{"id": v["id"], "sku": v["sku"], "price": v["price"]} for v in pub["variants"]],
                 "updated_at": pub["updated_at"]}]
    if feed_type == "variant":
        return [{"id": v["id"], "product_id": pub["id"], "product_name": pub["name"], "merchant": pub["merchant"],
                 "name": v["name"], "sku": v["sku"], "options": v["options"], "image": v["image"], "price": v["price"],
                 "availability": v["availability"], "url": pub["url"], "updated_at": pub["updated_at"]}
                for v in pub["variants"]]
    return [pub]


def jsonld_node(pub: dict) -> dict:
    node: dict = {"@type": "Product", "@id": f"urn:agentready:product:{pub['id']}", "name": pub["name"]}
    for k, v in (("description", pub["description"]), ("sku", pub["identifiers"]["sku"]), ("mpn", pub["identifiers"]["mpn"]),
                 ("url", pub["url"]), ("category", (pub["category"] or {}).get("path"))):
        if v:
            node[k] = v
    if pub["identifiers"]["gtin"]:
        node[{8: "gtin8", 12: "gtin12", 13: "gtin13", 14: "gtin14"}.get(len(pub["identifiers"]["gtin"]), "gtin")] = pub["identifiers"]["gtin"]
    if pub["brand"]:
        node["brand"] = {"@type": "Brand", "name": pub["brand"]}
    if pub["images"]:
        node["image"] = pub["images"]
    if pub["attributes"]:
        node["additionalProperty"] = [{"@type": "PropertyValue", "name": a["name"], "value": a["value"],
                                       **({"unitText": a["unit"]} if a["unit"] else {})} for a in pub["attributes"]]
    avail = {"available": "InStock", "out_of_stock": "OutOfStock", "preorder": "PreOrder", "backorder": "BackOrder",
             "discontinued": "Discontinued"}.get((pub.get("availability") or {}).get("status") or "")
    if pub.get("price"):
        offer = {"@type": "Offer", "price": pub["price"]["current"], "priceCurrency": pub["price"]["currency"],
                 "seller": {"@type": "Organization", "name": pub["merchant"]["name"]}}
        if pub["price"].get("valid_until"):
            offer["priceValidUntil"] = pub["price"]["valid_until"][:10]
        if avail:
            offer["availability"] = f"https://schema.org/{avail}"
        if pub["url"]:
            offer["url"] = pub["url"]
        node["offers"] = offer
    if pub["variants"]:
        node["hasVariant"] = [{"@type": "Product", "@id": f"urn:agentready:variant:{v['id']}", "name": v["name"] or pub["name"],
                               **({"sku": v["sku"]} if v["sku"] else {}),
                               **({"offers": {"@type": "Offer", "price": v["price"]["current"], "priceCurrency": v["price"]["currency"]}}
                                  if v.get("price") else {})} for v in pub["variants"]]
    return node


def _csv_safe(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else ",".join(map(str, v))
    v = str(v)
    return ("'" + v) if v[:1] in ("=", "+", "-", "@") else v  # spreadsheet formula injection


def agentready_csv_rows(records: list[dict], feed_type: str) -> list[dict]:
    rows = []
    for r in records:
        if feed_type == "variant":
            rows.append({"variant_id": r["id"], "product_id": r["product_id"], "product_name": r["product_name"],
                         "name": r["name"], "sku": r["sku"],
                         "options": ";".join(f"{k}={v}" for k, v in (r["options"] or {}).items()),
                         "price_currency": (r["price"] or {}).get("currency"), "price_current": (r["price"] or {}).get("current"),
                         "availability": (r["availability"] or {}).get("status"), "url": r["url"], "updated_at": r["updated_at"]})
            continue
        if feed_type in ("availability", "pricing"):
            rows.append({"id": r["id"], "sku": r["sku"],
                         **({"availability": (r["availability"] or {}).get("status")} if feed_type == "availability" else
                            {"price_currency": (r["price"] or {}).get("currency"), "price_regular": (r["price"] or {}).get("regular"),
                             "price_current": (r["price"] or {}).get("current"), "on_sale": (r["price"] or {}).get("on_sale")}),
                         "variant_count": len(r["variants"]), "updated_at": r["updated_at"]})
            continue
        price = r.get("price") or {}
        row = {"id": r["id"], "merchant_id": r["merchant"]["id"], "name": r["name"], "description": r["description"],
               "brand": r["brand"], "sku": r["identifiers"]["sku"], "gtin": r["identifiers"]["gtin"], "mpn": r["identifiers"]["mpn"],
               "category": (r["category"] or {}).get("path"), "url": r["url"], "image": (r["images"] or [None])[0],
               "additional_images": " ".join(r["images"][1:]), "price_currency": price.get("currency"),
               "price_regular": price.get("regular"), "price_current": price.get("current"), "on_sale": price.get("on_sale"),
               "availability": (r.get("availability") or {}).get("status"), "variant_count": len(r["variants"]),
               "updated_at": r["updated_at"]}
        for a in r["attributes"]:
            row[f"attr:{a['key']}"] = f"{a['value']} {a['unit']}".strip() if a["unit"] else a["value"]
        rows.append(row)
    return rows


def to_csv(rows: list[dict]) -> str:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_csv_safe(r.get(c)) for c in cols])
    return buf.getvalue()


def _xml_value(tag: str, v) -> str:
    if isinstance(v, dict):
        inner = "".join(_xml_value(k if k.replace("_", "").isalnum() else "field", x) for k, x in v.items())
        return f"<{tag}>{inner}</{tag}>"
    if isinstance(v, list):
        return f"<{tag}>" + "".join(_xml_value("item", x) for x in v) + f"</{tag}>"
    if v is None:
        return f"<{tag}/>"
    return f"<{tag}>{escape(str(v))}</{tag}>"


def render(feed: FeedConfiguration, prof: DistributionProfile, seller: str, merchant: dict, pubs: list[dict],
           removed: list[dict], version: int, at: datetime) -> tuple[str, int, list[str], list[str]]:
    """Returns (content, record count, validation errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    meta = {"schema": FEED_SCHEMA, "product_schema": SCHEMA, "feed": {"id": str(feed.id), "name": feed.name,
            "type": feed.feed_type, "profile": feed.profile, "version": version}, "merchant": merchant,
            "generated_at": at.isoformat()}
    if feed.profile in ("openai", "google"):
        rows = []
        for pub in pubs:
            r, e, w = openai_rows(pub, prof, seller) if feed.profile == "openai" else google_rows(pub, prof)
            rows += r
            errors += e
            warnings += w
        if feed.format == "jsonl":
            return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), len(rows), errors, warnings
        if feed.format == "csv":
            return to_csv(rows), len(rows), errors, warnings
        items = []
        for r in rows:
            fields = "".join(f"<g:{k}>{escape(str(v))}</g:{k}>" if k not in ("title", "description", "link")
                             else f"<{k}>{escape(str(v))}</{k}>" for k, v in r.items())
            items.append(f"<item>{fields}</item>")
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">'
               f"<channel><title>{escape(seller)}</title><link>{escape(prof.public_url or '')}</link>"
               f"<description>{escape(feed.name)}</description>{''.join(items)}</channel></rss>\n")
        return xml, len(rows), errors, warnings
    records = [rec for pub in pubs for rec in compact(pub, feed.feed_type)]
    if feed.format == "json":
        doc = {**meta, "item_count": len(records), "items": records}
        if feed.feed_type == "incremental":
            doc["removed"] = removed
        return json.dumps(doc, ensure_ascii=False, indent=2, default=str), len(records), errors, warnings
    if feed.format == "jsonl":
        lines = [json.dumps(r, ensure_ascii=False, default=str) for r in records] + [json.dumps(t) for t in removed]
        return "".join(line + "\n" for line in lines), len(records), errors, warnings
    if feed.format == "csv":
        return to_csv(agentready_csv_rows(records, feed.feed_type)), len(records), errors, warnings
    if feed.format == "jsonld":
        doc = {"@context": "https://schema.org", "@type": "ItemList", "name": f"{seller} — {feed.name}",
               "dateModified": at.isoformat(), "numberOfItems": len(pubs),
               "itemListElement": [{"@type": "ListItem", "position": i + 1, "item": jsonld_node(p)} for i, p in enumerate(pubs)]}
        return json.dumps(doc, ensure_ascii=False, indent=2), len(pubs), errors, warnings
    body = "".join(_xml_value("record", r) for r in records)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<feed schema="{FEED_SCHEMA}" version="{version}" '
           f'generated_at="{at.isoformat()}" merchant="{escape(merchant["id"])}">{body}</feed>\n')
    return xml, len(records), errors, warnings


# ------------------------------------------------------------------ generation
async def create_snapshot(db: AsyncSession, feed: FeedConfiguration, trigger: str = "manual") -> FeedSnapshot:
    ver = (await db.execute(select(func.max(FeedSnapshot.version)).where(FeedSnapshot.feed_id == feed.id))).scalar_one() or 0
    snap = FeedSnapshot(business_id=feed.business_id, feed_id=feed.id, version=ver + 1, status="queued", trigger=trigger)
    db.add(snap)
    await db.flush()
    return snap


async def run_snapshot(db: AsyncSession, snapshot_id: uuid.UUID) -> FeedSnapshot:
    claim = await db.execute(update(FeedSnapshot).where(FeedSnapshot.id == snapshot_id, FeedSnapshot.status == "queued")
                             .values(status="running", started_at=now_utc()))
    await db.commit()
    snap = await db.get(FeedSnapshot, snapshot_id, populate_existing=True)
    if claim.rowcount != 1:
        return snap
    feed = await db.get(FeedConfiguration, snap.feed_id)
    business = await db.get(Business, snap.business_id)
    try:
        prof = (await db.execute(select(DistributionProfile).where(DistributionProfile.business_id == business.id))).scalar_one_or_none()
        ok, reason = channel_open(prof, "feeds")
        if not ok:
            raise FeedError(reason)
        scope = await merchant_scope(db, business, prof)
        at = now_utc()
        stmt = select(Product.id).where(eligibility_clause(scope, "feeds"))
        if feed.feed_type == "category" and feed.category_id:
            root = scope.category_paths.get(feed.category_id)
            ids = {feed.category_id} | ({c for c, p in scope.category_paths.items() if root and p.lower().startswith(root.lower() + " / ")})
            stmt = stmt.where(Product.category_id.in_(list(ids)))
        ids = list((await db.execute(stmt.order_by(Product.id))).scalars().all())
        snap.total = len(ids)
        await db.commit()
        pubs, manifest = [], {}
        for i in range(0, len(ids), CHUNK):
            rows = (await db.execute(select(Product).where(Product.id.in_(ids[i:i + CHUNK])).order_by(Product.id))).scalars().all()
            for p in rows:
                pub = public_product(p, scope, at)
                manifest[pub["id"]] = content_hash(pub)
                pubs.append(pub)
            snap.processed = min(len(ids), i + CHUNK)
            await db.commit()  # real progress for the UI
        removed: list[dict] = []
        if feed.feed_type == "incremental":
            prev = (await db.execute(select(FeedSnapshot).where(
                FeedSnapshot.feed_id == feed.id, FeedSnapshot.status == "completed", FeedSnapshot.id != snap.id)
                .order_by(FeedSnapshot.version.desc()).limit(1))).scalar_one_or_none()
            if prev is not None:
                base = prev.manifest or {}
                snap.since = prev.finished_at
                pubs = [p for p in pubs if base.get(p["id"]) != manifest[p["id"]]]
                gone = [pid for pid in base if pid not in manifest]
                if gone:
                    gone_rows = {str(p.id): p for p in (await db.execute(select(Product).where(
                        Product.id.in_([uuid.UUID(g) for g in gone])))).scalars().all()}
                    for g in gone:
                        p = gone_rows.get(g)
                        removed.append({"id": g, "removed": True, "reason": removal_reason(p) if p else "deleted",
                                        "at": at.isoformat()})
        merchant = scope.merchant_ref()
        content, n, errors, warnings = render(feed, prof, merchant["name"] or business.name, merchant, pubs, removed,
                                              snap.version, at)
        data = content.encode("utf-8")
        if len(data) > MAX_FEED_BYTES:
            raise FeedError(f"The feed is larger than {MAX_FEED_BYTES // 1_000_000} MB")
        ext = {"jsonld": "jsonld", "jsonl": "jsonl"}.get(feed.format, feed.format)
        snap.content, snap.content_type, snap.size_bytes = content, CONTENT_TYPES[feed.format], len(data)
        snap.filename = f"{merchant['id']}-{feed.feed_type}-v{snap.version}.{ext}"
        snap.checksum = hashlib.sha256(data).hexdigest()
        snap.manifest = manifest
        snap.record_count, snap.product_count, snap.removed_count = n, len(pubs), len(removed)
        snap.excluded_count = len(errors)
        snap.validation = {"valid": not errors, "errors": errors[:500], "warnings": warnings[:500],
                           "error_count": len(errors), "warning_count": len(warnings)}
        snap.status, snap.finished_at = "completed", now_utc()
        snap.expires_at = snap.finished_at + timedelta(hours=max(1, feed.max_age_hours))
        feed.latest_snapshot_id, feed.last_generated_at = snap.id, snap.finished_at
        analytics.record(db, business.id, "feed_generated", "feeds", analytics.Consumer("merchant", None, snap.trigger),
                         status_code=200, result_count=n, meta={"feed": feed.name, "version": snap.version,
                                                                "excluded": len(errors)})
        record(db, business.id, Actor.system("Feed generator"), "feed.generated", "feed", feed.id,
               f"Feed '{feed.name}' v{snap.version} generated: {n} records" + (f", {len(errors)} excluded" if errors else ""),
               {"version": snap.version, "records": n, "removed": len(removed), "excluded": len(errors)})
        await db.commit()
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the snapshot
        await db.rollback()
        snap = await db.get(FeedSnapshot, snapshot_id, populate_existing=True)
        snap.status, snap.finished_at = "failed", now_utc()
        snap.error = str(e) if isinstance(e, FeedError) else f"Feed generation failed ({type(e).__name__})"
        if not isinstance(e, FeedError):
            log.exception("feed generation failed")
        analytics.record(db, snap.business_id, "feed_failed", "feeds", analytics.Consumer("merchant", None, snap.trigger),
                         status_code=500, meta={"error": snap.error})
        await db.commit()
    return snap


def dispatch_snapshot(snapshot_id: uuid.UUID) -> str:
    if get_settings().job_backend == "celery":
        from app.workers.celery_app import generate_feed_task

        generate_feed_task.delay(str(snapshot_id))
        return "celery"
    from app.db.session import session_factory

    async def go():
        async with session_factory()() as s:
            await run_snapshot(s, snapshot_id)

    task = asyncio.get_running_loop().create_task(go())
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)
    return "inline"


async def wait_for_inline() -> None:
    while _inline_tasks:
        await asyncio.gather(*list(_inline_tasks), return_exceptions=True)


async def generate_due_feeds(db: AsyncSession, at: datetime | None = None, limit: int = 20) -> dict:
    """Scheduled generation. Each due configuration is claimed with a conditional UPDATE (no double runs)."""
    at = at or now_utc()
    due = (await db.execute(select(FeedConfiguration.id, FeedConfiguration.next_run_at, FeedConfiguration.schedule).where(
        FeedConfiguration.archived_at.is_(None), FeedConfiguration.next_run_at.is_not(None),
        FeedConfiguration.next_run_at <= at).limit(limit))).all()
    started = 0
    for fid, when, schedule in due:
        claim = await db.execute(update(FeedConfiguration).where(FeedConfiguration.id == fid, FeedConfiguration.next_run_at == when)
                                 .values(next_run_at=next_run(schedule, at)))
        await db.commit()
        if claim.rowcount != 1:
            continue
        feed = await db.get(FeedConfiguration, fid)
        snap = await create_snapshot(db, feed, "scheduled")
        await db.commit()
        await run_snapshot(db, snap.id)
        started += 1
    return {"generated": started}


# ------------------------------------------------------------------ tokens
async def issue_token(db: AsyncSession, feed: FeedConfiguration, name: str, expires_at=None, rate: int = 60,
                      rotated_from: FeedToken | None = None) -> tuple[FeedToken, str]:
    secret = generate_opaque_token("arf_")
    tok = FeedToken(business_id=feed.business_id, feed_id=feed.id, name=name, prefix=secret[:10],
                    token_hash=hash_opaque_token(secret), expires_at=expires_at, rate_limit_per_minute=rate,
                    rotated_from_id=rotated_from.id if rotated_from else None)
    db.add(tok)
    await db.flush()
    return tok, secret


def token_active(t: FeedToken) -> bool:
    return t.revoked_at is None and (t.expires_at is None or aware(t.expires_at) > now_utc())


def feed_url(secret: str) -> str:
    return f"{get_settings().api_base_url.rstrip('/')}/api/v3/feeds/{secret}"


def token_dict(t: FeedToken) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(t.id), "feed_id": str(t.feed_id), "name": t.name, "prefix": t.prefix,
            "url_hint": f"{get_settings().api_base_url.rstrip('/')}/api/v3/feeds/{t.prefix}…",
            "rate_limit_per_minute": t.rate_limit_per_minute, "expires_at": iso(t.expires_at), "revoked_at": iso(t.revoked_at),
            "last_used_at": iso(t.last_used_at), "usage_count": t.usage_count, "active": token_active(t),
            "rotated_from_id": str(t.rotated_from_id) if t.rotated_from_id else None, "created_at": iso(t.created_at)}


def snapshot_dict(s: FeedSnapshot) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(s.id), "feed_id": str(s.feed_id), "version": s.version, "status": s.status, "trigger": s.trigger,
            "total": s.total, "processed": s.processed, "record_count": s.record_count, "product_count": s.product_count,
            "removed_count": s.removed_count, "excluded_count": s.excluded_count, "size_bytes": s.size_bytes,
            "checksum": s.checksum, "filename": s.filename, "validation": s.validation or {}, "error": s.error,
            "created_at": iso(s.created_at), "started_at": iso(s.started_at), "finished_at": iso(s.finished_at),
            "expires_at": iso(s.expires_at), "since": iso(s.since),
            "expired": bool(s.expires_at and aware(s.expires_at) <= now_utc())}


def feed_dict(f: FeedConfiguration, latest: FeedSnapshot | None = None, tokens: list[FeedToken] | None = None) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(f.id), "name": f.name, "profile": f.profile, "format": f.format, "feed_type": f.feed_type,
            "category_id": str(f.category_id) if f.category_id else None, "schedule": f.schedule,
            "max_age_hours": f.max_age_hours, "next_run_at": iso(f.next_run_at), "last_generated_at": iso(f.last_generated_at),
            "archived_at": iso(f.archived_at), "latest": snapshot_dict(latest) if latest else None,
            "tokens": [token_dict(t) for t in tokens or []], "created_at": iso(f.created_at)}
