"""Measure extraction quality against the manually verified ground truth.

    python -m eval.run_eval                    # deterministic only (no AI)
    python -m eval.run_eval --provider mock    # mock AI path (NOT a real model)
    python -m eval.run_eval --provider groq    # real Groq (needs GROQ_API_KEY)

The fixture website is served from memory; with --provider groq the AI calls
go to the real Groq API. Results are written to eval/results/<provider>.json.
Numbers produced here are the only accuracy figures AgentReady should quote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _setup_env(provider: str) -> None:
    os.environ.setdefault("SECRET_KEY", "eval-secret-key-not-for-production-0000")
    os.environ["ENVIRONMENT"] = "test"
    os.environ["AI_PROVIDER"] = provider
    os.environ["CRAWL_DELAY_SECONDS"] = "0"
    os.environ["JOB_BACKEND"] = "inline"
    os.environ["LOG_LEVEL"] = "WARNING"


def _norm(s):
    from app.services.normalization.normalize import normalize_name_for_match

    return normalize_name_for_match(s)


async def run(provider: str) -> dict:
    from sqlalchemy import select

    from app.db import session as db_session
    from app.db.base import Base
    from app.models import AIRequest, Business, CrawledPage, Product, ScanJob, User, ValidationIssue, Website
    from app.services.crawler import fetcher as fetcher_mod
    from app.tests.sitekit import FixtureSite, public_resolver
    from app.workers.scan_runner import run_scan

    gt = json.loads((ROOT / "ground_truth" / "shop.json").read_text())
    site = FixtureSite()
    fetcher_mod.TRANSPORT_OVERRIDE = site.transport()
    fetcher_mod.RESOLVER_OVERRIDE = public_resolver

    tmp = tempfile.mkdtemp()
    engine = db_session.set_engine(f"sqlite+aiosqlite:///{tmp}/eval.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = db_session.session_factory()
    async with Session() as s:
        u = User(email="eval@example.com", password_hash="x")
        s.add(u)
        await s.flush()
        b = Business(owner_id=u.id, name="Eval")
        s.add(b)
        await s.flush()
        w = Website(business_id=b.id, url="https://shop.example", normalized_url="https://shop.example/",
                    domain="shop.example", max_pages=50, max_depth=3)
        s.add(w)
        await s.flush()
        scan = ScanJob(business_id=b.id, website_id=w.id, settings={}, stage_history=[], errors=[], change_summary={})
        s.add(scan)
        await s.commit()
        scan_id, bid = scan.id, b.id

    t0 = time.monotonic()
    await run_scan(scan_id)
    elapsed = time.monotonic() - t0

    async with Session() as s:
        scan = await s.get(ScanJob, scan_id)
        pages = (await s.execute(select(CrawledPage).where(CrawledPage.scan_id == scan_id))).scalars().all()
        products = (await s.execute(select(Product).where(Product.business_id == bid))).scalars().all()
        issues = (await s.execute(select(ValidationIssue).where(ValidationIssue.business_id == bid,
                                                                ValidationIssue.status == "open"))).scalars().all()
        ai = (await s.execute(select(AIRequest).where(AIRequest.business_id == bid))).scalars().all()

    # ---------------------------------------------------------- discovery
    predicted = {p.url.replace("https://shop.example", "") or "/": p.page_type for p in pages}
    truth_pos = set(gt["pages"]["product"]) | set(gt["pages"]["listing"])
    pred_pos = {u for u, t in predicted.items() if t in ("product", "listing")}
    tp = len(truth_pos & pred_pos)
    discovery = {
        "precision": round(tp / len(pred_pos), 3) if pred_pos else None,
        "recall": round(tp / len(truth_pos), 3) if truth_pos else None,
        "false_positives": sorted(pred_pos - truth_pos), "missed": sorted(truth_pos - pred_pos),
    }

    # ---------------------------------------------------------- products & fields
    by_name: dict[str, list] = {}
    for p in products:
        by_name.setdefault(_norm(p.name), []).append(p)
    field_hits = field_total = price_hits = price_total = attr_hits = attr_total = 0
    per_product = []
    found = 0
    for exp in gt["products"]:
        cands = by_name.get(_norm(exp["name"]), [])
        row = {"name": exp["name"], "found": bool(cands), "fields": {}}
        if cands:
            found += 1
            p = cands[0]
            for f in ("sku", "brand", "currency", "availability", "gtin", "category"):
                if f in exp:
                    ok = getattr(p, f) == exp[f]
                    row["fields"][f] = ok
                    field_hits += ok
                    field_total += 1
            if exp.get("price_conflict"):
                flagged = any(i.code == "conflicting_values" and i.field == "price" and i.product_id == p.id for i in issues)
                row["fields"]["price_conflict_flagged"] = flagged
                price_hits += flagged
                price_total += 1
            elif "price" in exp:
                got = float(p.price_amount) if p.price_amount is not None else None
                ok = got == exp["price"]
                row["fields"]["price"] = ok
                price_hits += ok
                price_total += 1
            for k, (v, unit) in (exp.get("attributes") or {}).items():
                a = next((a for a in p.attributes if a.key == k), None)
                ok = a is not None and a.value == v and a.unit == unit
                row["fields"][f"attr:{k}"] = ok
                attr_hits += ok
                attr_total += 1
        else:
            field_total += sum(1 for f in ("sku", "brand", "currency", "availability", "gtin", "category") if f in exp)
            price_total += 1 if ("price" in exp or exp.get("price_conflict")) else 0
            attr_total += len(exp.get("attributes") or {})
        per_product.append(row)
    expected_names = {_norm(e["name"]) for e in gt["products"]}
    spurious = sorted({p.name for p in products if _norm(p.name) not in expected_names})

    # ---------------------------------------------------------- duplicates
    dup_codes = [i for i in issues if i.code == "possible_duplicate"]
    dup_pairs_found = {frozenset({str(i.product_id), str(i.details.get("other_product_id"))}) for i in dup_codes}
    expected_dup = 0
    for a, b in gt["duplicate_pairs"]:
        ids_a = [p.id for p in by_name.get(_norm(a), [])]
        ids_b = [p.id for p in by_name.get(_norm(b), [])]
        hit = any(frozenset({str(x), str(y)}) in dup_pairs_found for x in ids_a for y in ids_b if x != y)
        expected_dup += hit
    # ---------------------------------------------------------- cost
    tokens = sum(r.total_tokens for r in ai)
    report = {
        "provider": provider,
        "model": next((r.model for r in ai), None),
        "scan_status": scan.status,
        "processing_seconds": round(elapsed, 2),
        "pages_crawled": scan.pages_crawled,
        "discovery": discovery,
        "product_recall": round(found / len(gt["products"]), 3),
        "spurious_products": spurious,
        "field_accuracy": round(field_hits / field_total, 3) if field_total else None,
        "price_accuracy": round(price_hits / price_total, 3) if price_total else None,
        "attribute_accuracy": round(attr_hits / attr_total, 3) if attr_total else None,
        "duplicate_detection_recall": round(expected_dup / len(gt["duplicate_pairs"]), 3),
        "duplicate_candidates_flagged": len(dup_pairs_found),
        "catalog_completeness_avg": round(sum(p.quality_score for p in products) / len(products), 1) if products else None,
        "human_review_rate": round(sum(1 for p in products if p.quality_status != "valid") / len(products), 3) if products else None,
        "ai_requests": len(ai),
        "ai_tokens_total": tokens,
        "ai_tokens_per_100_products": round(tokens / len(products) * 100) if products and tokens else 0,
        "ai_estimated_cost_usd": round(sum(r.estimated_cost_usd for r in ai), 6),
        "per_product": per_product,
        "dataset": {"products": len(gt["products"]), "pages": sum(len(v) for v in gt["pages"].values()),
                    "note": "single small fixture site; not representative of real-world accuracy"},
    }
    await engine.dispose()
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["none", "mock", "groq"], default="none")
    args = ap.parse_args()
    _setup_env(args.provider)
    sys.path.insert(0, str(ROOT.parent))
    report = asyncio.run(run(args.provider))
    out = ROOT / "results" / f"{args.provider}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    summary = {k: v for k, v in report.items() if k not in ("per_product",)}
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nFull report: {out}")


if __name__ == "__main__":
    main()
