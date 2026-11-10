# AgentReady architecture

AgentReady turns a merchant's existing website into a validated, merchant-controlled product catalog and
distributes it to AI agents (REST, MCP, feeds, webhooks) under the merchant's control, with catalog intelligence
on top. This document describes how the system is put together; [INSTALLATION.md](INSTALLATION.md) and
[DEPLOYMENT.md](DEPLOYMENT.md) describe how to run it.

## Runtime components

```
                    ┌──────────────── browser (React SPA) ────────────────┐
                    │  merchant UI: catalog, commerce, distribution, ...  │
                    └───────────────────────────┬─────────────────────────┘
                                                │ HTTPS
 AI agents / MCP clients / feed readers ──────▶ │
                                    ┌───────────▼───────────┐
                                    │ nginx (web container) │  static SPA, CSP + security headers,
                                    │                       │  TLS (production), /api proxy, log redaction
                                    └───────────┬───────────┘
                                                │ HTTP (internal network)
                                    ┌───────────▼───────────┐
                                    │ FastAPI API (uvicorn) │  /api/v1 merchant API, /api/v2 catalog API,
                                    │                       │  /api/v3 agents + MCP + feeds, /health
                                    └──┬──────────────┬─────┘
                                       │              │ enqueue jobs / rate limits / cache
                          SQL (asyncpg)│        ┌─────▼─────┐
                                       │        │   Redis   │  Celery broker, rate-limit counters,
                                       │        └─────┬─────┘  public-response cache, scheduler heartbeat
                              ┌────────▼───────┐      │
                              │  PostgreSQL 16  │◀─────┤
                              └────────▲───────┘      │
                                       │        ┌─────▼──────────┐   ┌──────────────────┐
                                       └────────┤ Celery worker  │   │ Celery Beat (x1) │
                                                │ scans, feeds,  │◀──┤ scheduler tick   │
                                                │ bulk, imports, │   │ (60 s), webhook  │
                                                │ webhooks, intel│   │ tick (10 s)      │
                                                └───────┬────────┘   └──────────────────┘
                                                        │ outbound HTTP through the SSRF guard
                                   merchant websites · WooCommerce/Shopify · webhook receivers · Groq API
```

| Component | Technology | Responsibility |
|---|---|---|
| Web | React 18, TypeScript, Vite, Tailwind, React Query, served by nginx 1.27 | Merchant UI; route-level code splitting |
| API | FastAPI, async SQLAlchemy 2, Pydantic 2, uvicorn | All HTTP APIs; authentication; validation |
| Worker | Celery 5 | Scans, feed generation, bulk operations, integration imports, webhook delivery, intelligence |
| Scheduler | Celery Beat (exactly one) | Enqueues the scheduler tick every minute and the webhook tick every 10 s |
| Database | PostgreSQL 16 (SQLite supported for local development and tests) | Source of truth; Alembic migrations 0001–0006 |
| Queue/cache | Redis 7 (append-only persistence) | Celery broker, rate limits, response cache, scheduler heartbeat |
| AI | Groq REST API via `httpx` (no SDK) | Extraction fallback, assistant phrasing, description suggestions |

With `JOB_BACKEND=inline` (local development only) jobs run as asyncio tasks inside the API process and an
in-process loop replaces Beat; Redis is then not needed.

## Backend layout

```
backend/app/
  api/v1/        merchant API: auth, websites, scans, products, pricing/inventory, commerce, sync, integrations,
                 distribution, intelligence, exports, settings, dashboard, system (status)
  api/v2/        scoped catalog API (agentready-catalog/2.0)
  api/v3/        agent API (agents.py), MCP Streamable HTTP server (mcp.py), public feed URLs (feeds.py)
  core/          config + production validation, security (bcrypt/JWT), errors, rate limiting,
                 observability (logging, redaction, request context), health checks
  db/            engine/session (pool settings), portable column types, wait-for-database
  models/        SQLAlchemy models (Phase 1 core, commerce, distribution, intelligence)
  schemas/       request/response schemas (extra="forbid" against mass assignment), canonical product schema
  services/      crawler · discovery · extraction · ai · normalization · validation · deduplication · catalog ·
                 export · commerce · integrations · distribution · intelligence
  workers/       Celery app, scan runner, dispatch, scheduler tick, interrupted-job recovery
```

## Data flow through the product

1. **Connect & crawl.** `POST /websites` + `POST /scans` create a `scan_jobs` row; the worker runs
   `ScanRunner`: robots.txt + sitemaps + bounded BFS through `SafeFetcher` (SSRF guard, pinned IPs, size limits).
2. **Classify & extract.** Each page is classified (product / listing / other). Extraction order: JSON-LD →
   microdata → meta → table engine → HTML heuristics → product cards; Groq is called only when deterministic
   output is insufficient. Every value gets a `product_evidence` row (source URL, fragment, method, model).
3. **Normalize & validate.** Prices (Decimal), currencies, units, availability and identifiers are normalized;
   `validation_issues` and a quality status are computed; possible duplicates are surfaced, never merged.
4. **Review & approve.** First scan populates the catalog; rescans produce a change report the merchant applies.
   Merchant edits are recorded in `manual_overrides` and never overwritten; disagreements become `sync_conflicts`.
5. **Manage.** Pricing (immutable `price_history`, scheduled changes), inventory (transaction log, optimistic
   concurrency via `inventory_version`), variants, categories, attribute definitions, bulk operations.
6. **Distribute.** One public serializer (`services/distribution/public.py`, `agentready-product/1.0`) produces
   every external representation: agent API, MCP tools, feeds, webhook payloads, the merchant's preview.
   A `before_flush` hook writes product changes to a transactional outbox → webhook deliveries.
7. **Observe.** Agent requests, MCP calls and feed downloads are recorded in `distribution_events`.
8. **Improve.** The intelligence engine computes per-product quality results (incrementally), daily snapshots,
   discovery insights (from `distribution_events`), recommendations, anomalies, notifications and reports. The AI
   assistant answers from SQL-retrieved facts; suggested edits apply only after merchant approval.

## Key design rules

* **Tenant isolation.** Every merchant row carries `business_id`; every query filters on the authenticated
  merchant. Tested per phase (`test_*_isolation`).
* **Nothing private leaves through public surfaces.** The public serializer never includes cost prices, stock
  quantities, reserved stock, locations, notes, evidence, approval internals or credentials; private attribute
  keys (`supplier`, `cost`, `margin`, `notes`, …) are dropped from the agent API, MCP, feeds and the v1/v2 catalog
  APIs. Cross-surface sentinel test: `test_production.py::test_private_merchant_data_never_reaches_public_surfaces`.
* **Jobs are claimed, never double-run.** Scans, feed snapshots, scheduled price changes, scheduled rescans, webhook
  deliveries and per-merchant intelligence passes are claimed with conditional `UPDATE`s. Interrupted jobs are
  recovered by the scheduler (`workers/recovery.py`): resumed when idempotent, otherwise marked failed with a reason.
* **No fabricated metrics.** Dashboards and reports use only recorded data; unavailable metrics are `null`.
* **AI is grounded.** Extraction output is checked against page text; assistant answers containing numbers or
  product references not present in the retrieved facts are rejected in favour of the deterministic answer.

## Database

Migrations live in `backend/alembic/versions` (0001 initial schema → 0006 production readiness). All are additive
or create new tables; each has a downgrade. `alembic check` reports no drift between models and migrations.
Money is `NUMERIC(14,2)`; timestamps are `timestamptz` (a portable `UTCDateTime` type keeps SQLite aware).
JSON columns use JSONB on PostgreSQL. Indexes cover the hot paths (`business_id` + status/updated/lifecycle on
products, per-merchant event and report indexes, unique constraints for one-per-merchant rows).

## Observability

* `GET /health/live` (liveness) and `GET /health` (readiness: database, Redis, worker ping, scheduler heartbeat).
* Every response carries `X-Request-ID` (nginx's `$request_id` is propagated) and `Server-Timing`; one structured
  access log line per request with `duration_ms`. `LOG_FORMAT=json` for log shippers.
* Secrets are redacted from every log record (`core/observability.py`).
* Merchant-facing job visibility: **System status** page (`GET /api/v1/system/status`).

More detail per area: [API_DOCUMENTATION.md](API_DOCUMENTATION.md), [MCP_INTEGRATION.md](MCP_INTEGRATION.md),
[SECURITY.md](SECURITY.md), [docs/feed-format.md](docs/feed-format.md), [docs/phase3-platforms.md](docs/phase3-platforms.md).
