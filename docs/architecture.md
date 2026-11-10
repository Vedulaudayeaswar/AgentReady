# Architecture

## Components

| Component | Tech | Responsibility |
|---|---|---|
| Web app | React, Vite, TS, Tailwind, Radix/shadcn-style UI, TanStack Query, RHF + Zod, Recharts | Landing, auth, merchant dashboard |
| API | FastAPI, Pydantic, SQLAlchemy 2 (async) | REST API, auth, merchant isolation, scan dispatch |
| Worker | Celery + Redis (or in-process for dev) | Runs scans |
| Database | PostgreSQL (UUID PKs, JSONB) | Catalog, evidence, issues, snapshots, exports, AI request log |

## Scan pipeline (`app/workers/scan_runner.py`)

Stages are written to `scan_jobs.stage_history` as they actually start/finish:

1. **connecting** — SSRF validation, homepage fetch.
2. **discovering_pages** — robots.txt, sitemaps (indexes followed), BFS of internal links within depth/page limits.
3. **finding_products** — `discovery.classifier` scores each page; reasons are stored on `crawled_pages.signals`.
4. **extracting** — `extraction.pipeline.PageExtractor`: structured data → tables → heuristics → cards, with
   `AIGateway` called only when needed. Ambiguous tables send only the table text to the model.
5. **normalizing** — `normalization.product.finalize` + within-scan de-duplication by match key.
6. **validating** — snapshots and a change report against the existing catalog (`catalog.service.build_snapshots`).
7. **generating_catalog** — first scans are applied directly; rescans with changes wait in `awaiting_review` until the
   merchant applies them (`POST /scans/{id}/apply`). Validation (`validation.validator.revalidate_business`) runs after
   apply and after every merchant edit.

## Identity across scans

`match_key` priority: SKU → GTIN → dedicated product URL → normalized name. Matching for rescans also falls back to
each identifier independently. Products are only marked *removed* when their source page was re-crawled (or returned
404/410) and the product is no longer there, so a smaller crawl budget never deletes products.

## AI abstraction

`services/ai/base.py` defines `AIProvider` and provider-independent response models. `GroqExtractionService` (httpx)
and `MockExtractionProvider` implement it. `AIGateway` adds budgets, caching, logging and cost estimation, and is the
only thing the extraction pipeline talks to. `services/ai/convert.py` converts AI output to the canonical schema and
verifies every value against the source text.

## Extending to later phases

* **Feeds/integrations (Phase 3):** the `feed` exporter and `/catalog` API are versioned; new channel-specific
  exporters plug into `services/export`.
* **Sync (Phase 2):** scans are idempotent and produce snapshots; scheduled rescans can reuse `build_snapshots` and
  `apply_scan` with an auto-apply policy.
* **Analytics (Phase 4):** product ids and versions are stable, enabling event attribution.
