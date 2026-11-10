<div align="center">

# AgentReady

### Make every product ready for AI commerce

**Turn any existing product website into a validated, merchant-controlled, machine-readable catalog — and
distribute it to AI shopping agents through a REST agent API, an MCP server, product feeds and signed webhooks.**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-5-37814A?logo=celery&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-black)
![Tests](https://img.shields.io/badge/backend%20tests-170%20passing-brightgreen)

**Built by [Vedula Uday Easwar](https://github.com/Vedulaudayeaswar)**

</div>

---

## Table of contents

1. [The problem](#the-problem)
2. [What AgentReady does](#what-agentready-does)
3. [Highlights](#highlights)
4. [Tech stack](#tech-stack)
5. [Architecture](#architecture):
   [system](#system-architecture) · [scan pipeline](#scan--extraction-pipeline) ·
   [request lifecycle](#scan-request-lifecycle) · [AI distribution](#ai-distribution) ·
   [data model](#data-model) · [deployment](#deployment-topologies)
6. [Quick start](#quick-start-docker)
7. [Deploy to Render (free plan)](#deploy-to-render-free-plan)
8. [Feature deep dive](#feature-deep-dive) (Phases 1–5)
9. [Tests](#tests)
10. [Project structure](#project-structure)
11. [Documentation](#documentation)
12. [Author](#author)

---

## The problem

AI shopping assistants (ChatGPT, Gemini, Perplexity, custom MCP agents) can only recommend products they can
**read reliably**. Most small and mid-size merchants have product data locked inside HTML pages: inconsistent
prices, specs in tables, missing identifiers, no structured data. Rebuilding the website or hiring a data team is
not an option for them.

## What AgentReady does

```mermaid
flowchart LR
    A["Merchant enters<br/>website URL"] --> B["Crawl & classify<br/>pages"]
    B --> C["Extract products<br/>rules first, AI fallback"]
    C --> D["Normalize, validate,<br/>de-duplicate"]
    D --> E["Merchant reviews<br/>& approves"]
    E --> F["Manage catalog<br/>prices · stock · variants"]
    F --> G["Distribute to AI agents<br/>API · MCP · feeds · webhooks"]
    G --> H["Intelligence<br/>quality · insights · assistant"]
    H -. improves .-> F
```

| Phase | Module | What it delivers |
|---|---|---|
| 1 | **AI Catalog Generator** | crawler, page classifier, multi-stage extraction with evidence, Groq AI fallback with hallucination guards, validation, review/approve, exports (JSON, CSV, JSON-LD) |
| 2 | **Commerce Management** | catalog as source of truth: pricing with immutable history, transaction-safe inventory, variants, taxonomy, bulk operations, scheduled rescans with conflict resolution, WooCommerce/Shopify, scoped catalog API, audit log |
| 3 | **AI Commerce Distribution** | agent discovery API, MCP server, versioned feeds (OpenAI / Google profiles), signed webhooks via a transactional outbox, checkout hand-off, analytics from real events |
| 4 | **Commerce Intelligence** | 12-dimension quality score, discovery insights incl. zero-result analysis, recommendations, anomaly detection, reports, grounded AI assistant |
| 5 | **Production readiness** | config validation, real health checks, structured logs with redaction, throttling, CSP/HSTS, job recovery, TLS compose overlay, backup/restore |

## Highlights

Engineering decisions worth looking at:

* **AI is grounded, not trusted.** LLM output is schema-validated and every value is checked against the source
  page; prices that do not appear in the page are discarded. Assistant answers containing numbers not present in
  the SQL-retrieved facts are rejected. Page content is wrapped as untrusted data against prompt injection.
* **Full provenance.** Every field stores its source URL, HTML fragment, extraction method and model/prompt version.
* **SSRF-hardened outbound HTTP.** The crawler, store integrations and webhooks share one guard: DNS resolution
  with private-range blocking, pinned IPs, port allow-list, size and time limits, no blind redirects.
* **Concurrency-safe commerce.** `Decimal` money in `NUMERIC(14,2)`, optimistic locking on inventory, jobs claimed
  with conditional `UPDATE`s so nothing runs twice, append-only price and inventory logs.
* **One public serializer.** Every external surface (agent API, MCP, feeds, webhooks) goes through a single
  function that cannot emit cost prices, stock counts or private attributes, enforced by a cross-surface test.
* **Transactional outbox for webhooks.** A SQLAlchemy `before_flush` hook records changes atomically; deliveries
  are signed per *Standard Webhooks*, retried with jittered backoff and replayable.
* **Standards-compliant MCP server**, verified with the official MCP Python SDK across protocol versions.
* **Multi-tenant isolation** on every query, tested per phase.
* **No fabricated metrics.** Unavailable numbers are `null`, never `0`; extraction quality is measured against a
  ground-truth dataset, with an explicit caveat about its size.

## Tech stack

| Layer | Technologies |
|---|---|
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, Radix UI, TanStack Query, React Hook Form + Zod, Recharts |
| Backend | Python 3.12, FastAPI, async SQLAlchemy 2, Pydantic 2, Alembic, httpx, BeautifulSoup + lxml |
| Jobs | Celery 5 + Celery Beat on Redis, or in-process asyncio jobs for single-instance hosting |
| Data | PostgreSQL 16 (JSONB, `pg_trgm`); SQLite for local development and tests |
| AI | Groq REST API (`openai/gpt-oss-120b`) with JSON-schema structured output, no vendor SDK |
| Agent protocols | Model Context Protocol (Streamable HTTP), OpenAI Product Feed, Google Merchant feed, schema.org JSON-LD, Standard Webhooks |
| Security | bcrypt, JWT with revocation, Fernet-encrypted credentials, HMAC-signed webhooks, CSP/HSTS |
| Ops | Docker, Docker Compose (dev + production overlay), nginx 1.27 with TLS, Render Blueprint, backup/restore scripts |
| Testing | pytest (170 tests), Vitest, Playwright UI walkthroughs, evaluation harness with ground truth |

---

## Architecture

### System architecture

```mermaid
flowchart TB
    subgraph Clients
        M["Merchant<br/>(React SPA)"]
        AG["AI agents<br/>REST / MCP"]
        FR["Feed readers<br/>ChatGPT · Google"]
    end

    NG["nginx<br/>TLS · CSP · SPA · /api proxy"]

    subgraph Backend["FastAPI application"]
        V1["/api/v1<br/>merchant API"]
        V2["/api/v2<br/>scoped catalog API"]
        V3["/api/v3<br/>agents · MCP · feeds"]
        SVC["Services<br/>crawler · extraction · AI · normalization ·<br/>validation · commerce · distribution · intelligence"]
    end

    subgraph Async["Background processing"]
        W["Celery worker<br/>scans · feeds · bulk · imports · webhooks"]
        B["Celery Beat (x1)<br/>scheduler 60 s · webhooks 10 s"]
    end

    PG[("PostgreSQL 16")]
    RD[("Redis 7<br/>broker · rate limits · cache")]

    subgraph External
        WEB["Merchant websites"]
        STORE["Shopify · WooCommerce"]
        GROQ["Groq LLM API"]
        HOOK["Webhook receivers"]
    end

    M --> NG
    AG --> NG
    FR --> NG
    NG --> V1
    NG --> V2
    NG --> V3
    V1 --> SVC
    V2 --> SVC
    V3 --> SVC
    SVC --> PG
    SVC --> RD
    RD --> W
    B --> RD
    W --> PG
    W -- "SSRF guard" --> WEB
    W -- "SSRF guard" --> STORE
    W -- "SSRF guard" --> HOOK
    W --> GROQ
```

### Scan & extraction pipeline

```mermaid
flowchart TD
    U["Website URL"] --> S["SSRF check<br/>DNS resolve · block private ranges · pin IP"]
    S --> R["robots.txt + sitemaps"]
    R --> C["Bounded BFS crawler<br/>depth · page · size · delay limits"]
    C --> K{"Page classifier<br/>explainable signals"}
    K -- product --> J
    K -- listing --> PC["Product cards"] --> J
    K -- other --> SKIP["Skip"]
    J["1 · JSON-LD"] --> MD["2 · Microdata"] --> MT["3 · Meta tags"] --> T["4 · Table engine"] --> HH["5 · HTML heuristics"]
    HH --> Q{"Enough data?"}
    Q -- no --> AI["Groq LLM<br/>structured output"] --> G["Grounding guard<br/>value must appear in page"]
    Q -- yes --> N
    G --> N["Normalize<br/>Decimal prices · currencies · units · GTIN"]
    N --> D["Duplicate candidates<br/>never auto-merged"]
    D --> V["Validation issues + quality status"]
    V --> E["Evidence row per field"]
    E --> RV["Merchant review → approve → catalog"]
```

### Scan request lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Merchant
    participant UI as React SPA
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Redis / inline
    participant W as Worker
    participant Site as Merchant site
    participant LLM as Groq

    Merchant->>UI: Connect website + start scan
    UI->>API: POST /api/v1/scans
    API->>DB: insert scan_jobs (queued)
    API->>Q: enqueue scan
    API-->>UI: 202 + scan id
    W->>DB: claim job (conditional UPDATE)
    loop each permitted page
        W->>Site: GET via SafeFetcher
        W->>W: classify + extract
        opt deterministic result insufficient
            W->>LLM: chat/completions (JSON schema)
            LLM-->>W: candidate fields
            W->>W: grounding check against page text
        end
        W->>DB: products, evidence, issues, progress
    end
    UI->>API: GET /scans/{id}/progress (polling)
    API-->>UI: pages, products, AI usage
    Merchant->>UI: Review, edit, approve
    UI->>API: PATCH /products · bulk-approval
```

### AI distribution

```mermaid
flowchart LR
    P["Approved + published<br/>products"] --> PS
    DS["Distribution settings<br/>channels · withheld fields · exclusions"] --> PS
    PS["Public serializer<br/>agentready-product/1.0<br/>drops cost, stock counts, private attributes"]
    PS --> A["Agent API /api/v3/agents<br/>search · compare · recommend"]
    PS --> MCP["MCP server /api/v3/mcp<br/>9 read-only tools"]
    PS --> F["Feeds<br/>JSON · JSONL · CSV · XML · JSON-LD<br/>OpenAI · Google profiles"]
    P -- "before_flush hook" --> OB["Transactional outbox"]
    OB --> WH["Signed webhooks<br/>HMAC · retries · replay"]
    A --> EV["distribution_events"]
    MCP --> EV
    F --> EV
    EV --> IN["Intelligence<br/>discovery insights · zero-result analysis"]
```

### Data model

53 tables in total; the core relationships:

```mermaid
erDiagram
    BUSINESSES ||--o{ USERS : has
    BUSINESSES ||--o{ WEBSITES : owns
    WEBSITES ||--o{ SCAN_JOBS : "scanned by"
    SCAN_JOBS ||--o{ CRAWLED_PAGES : fetches
    SCAN_JOBS ||--o{ SCAN_PRODUCT_SNAPSHOTS : produces
    BUSINESSES ||--o{ PRODUCTS : catalogs
    PRODUCTS ||--o{ PRODUCT_VARIANTS : has
    PRODUCTS ||--o{ PRODUCT_ATTRIBUTES : has
    PRODUCTS ||--o{ PRODUCT_EVIDENCE : "proven by"
    PRODUCTS ||--o{ VALIDATION_ISSUES : flags
    PRODUCTS ||--o{ PRICE_HISTORY : "append-only"
    PRODUCTS ||--o{ INVENTORY_TRANSACTIONS : "append-only"
    CATEGORIES ||--o{ PRODUCTS : classifies
    PRODUCTS ||--o{ SYNC_CONFLICTS : "website vs merchant"
    BUSINESSES ||--o{ API_KEYS : issues
    BUSINESSES ||--o{ COMMERCE_INTEGRATIONS : connects
    BUSINESSES ||--o{ FEED_SNAPSHOTS : generates
    BUSINESSES ||--o{ WEBHOOK_SUBSCRIPTIONS : registers
    WEBHOOK_SUBSCRIPTIONS ||--o{ WEBHOOK_DELIVERIES : sends
    OUTBOX_EVENTS ||--o{ WEBHOOK_DELIVERIES : "fans out"
    BUSINESSES ||--o{ DISTRIBUTION_EVENTS : observes
    PRODUCTS ||--o{ PRODUCT_QUALITY_RESULTS : scored
    BUSINESSES ||--o{ INTELLIGENCE_RECOMMENDATIONS : receives
    BUSINESSES ||--o{ AUDIT_LOGS : records
```

### Deployment topologies

```mermaid
flowchart LR
    subgraph Prod["Self-hosted production: Docker Compose"]
        direction TB
        n["nginx :443 TLS"] --> api["FastAPI API"]
        mig["migrate (one-shot)"] --> pg[("PostgreSQL")]
        api --> pg
        api --> rd[("Redis")]
        wk["Celery worker"] --> rd
        bt["Celery Beat x1"] --> rd
        wk --> pg
    end
    subgraph Render["Render free plan: single service"]
        direction TB
        rs["Docker web service<br/>FastAPI + built SPA<br/>inline jobs + scheduler"] --> rpg[("Render PostgreSQL")]
    end
```

---

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: set SECRET_KEY (required) and GROQ_API_KEY (optional — without it, extraction is deterministic only)
docker compose up --build
```

| Service | URL |
|---|---|
| Web app | http://localhost:8080 |
| API + OpenAPI docs | http://localhost:8000/docs |
| Health (database, Redis, worker, scheduler) | http://localhost:8000/health |

The stack runs PostgreSQL 16, Redis 7, the FastAPI API (runs migrations on start), a Celery worker, a single Celery
Beat process (scheduled rescans, price changes, feed regeneration, intelligence and stuck-job recovery, ticking
every minute) and an nginx-served React build. For a public server use the production overlay
(`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`) described in [DEPLOYMENT.md](DEPLOYMENT.md).
Set `CREDENTIALS_ENCRYPTION_KEY` in `.env` before connecting stores (required in production; see `.env.example`). If port 5432 is unavailable on your machine set `POSTGRES_HOST_PORT=55432` (only affects host access).

## Local development (without Docker)

Backend (Python 3.12):

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
# backend/.env for local dev — SQLite + in-process jobs. The backend reads ONLY backend/.env;
# the repository-root .env is used by docker compose.
#   ENVIRONMENT=development
#   DATABASE_URL=sqlite+aiosqlite:///./agentready.db
#   JOB_BACKEND=inline
#   SECRET_KEY=<random string>
#   GROQ_API_KEY=<optional>
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Frontend (Node 20):

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173 — proxies /api to localhost:8000
```

`JOB_BACKEND=inline` runs scans as asyncio tasks inside the API process (development only). Use
`JOB_BACKEND=celery` with Redis and `celery -A app.workers.celery_app worker` for anything else.

## Deploy to Render (free plan)

The repository includes a [render.yaml](render.yaml) Blueprint. On the free plan it runs **one Docker web
service**: FastAPI serves both the API and the built React app, jobs and the scheduler run inside that process
(`JOB_BACKEND=inline`), and data lives in a free Render PostgreSQL database. No Redis or Celery worker is needed,
since Render's free tier has no background workers.

1. Fork or clone this repository to your GitHub account.
2. In Render: **New → Blueprint**, select the repository. Render reads `render.yaml`.
3. When prompted, fill in:
   * `CREDENTIALS_ENCRYPTION_KEY`: generate with
     `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`
   * `GROQ_API_KEY`: optional ([console.groq.com](https://console.groq.com)); without it, extraction is
     deterministic only.
4. Apply. The first build takes a few minutes; migrations run automatically on start.
5. Open `https://<your-service>.onrender.com/signup` and create the first merchant account.

`SECRET_KEY` is generated by Render. The public URL is taken from `RENDER_EXTERNAL_URL` and used for CORS and links.

**Free-plan limits:**

| Limit | Effect |
|---|---|
| Service sleeps after 15 min without traffic | the first request afterwards takes about a minute |
| 512 MB RAM, shared CPU | scans are capped at 50 pages / 250 products in `render.yaml` |
| Free PostgreSQL expires after 30 days (1 GB) | upgrade the database or recreate it; export the catalog first |
| Jobs run in the web process | a scan running during a restart or sleep is marked failed by stale-job recovery; run it again |

For a production setup, use the Docker Compose production overlay ([DEPLOYMENT.md](DEPLOYMENT.md)), or on Render
add a Key Value (Redis) instance and a background worker and set `JOB_BACKEND=celery`.

---

## Feature deep dive

### Catalog generation (Phase 1)

```
URL ─▶ SSRF-checked fetcher ─▶ robots.txt + sitemaps + BFS crawler
     ─▶ page classifier (product / listing / other, explainable signals)
     ─▶ extraction: JSON-LD ▸ microdata ▸ meta ▸ table engine ▸ HTML heuristics ▸ product cards
                    └─ Groq only when deterministic results are insufficient (or tables are ambiguous)
     ─▶ normalization (prices, currencies, units, availability, identifiers) + within-scan de-duplication
     ─▶ snapshots ▸ change report vs. existing catalog ▸ apply (first scan) / merchant review (rescans)
     ─▶ validation issues + quality status + duplicate candidates
     ─▶ review / edit / approve ─▶ exports + read-only catalog API
```

Key properties:

* **Provenance.** Every extracted field stores its source value, URL, page, HTML/text fragment, source type,
  extraction method, model ID and prompt version (for AI), and whether the value was found verbatim in the source.
* **AI is grounded, not trusted.** AI output is validated with Pydantic, then each value is checked against the page
  text. Prices whose amount does not appear in the source, currencies not supported by the source, availability without
  an explicit phrase, and unknown image URLs are discarded. Other unverifiable values are kept but flagged for review.
  Model "confidence" is never treated as verification.
* **Prompt-injection resistant.** Page content is wrapped as untrusted data, delimiter spoofing is neutralised, and the
  model has no tools, secrets or database access.
* **Merchant edits win.** Edited fields are recorded as merchant overrides and preserved across rescans; the change
  report shows when a rescan disagrees with an edit.
* **Nothing is merged automatically.** Possible duplicates (same SKU/GTIN/URL or near-identical names) are surfaced
  for review.


### Commerce management (Phase 2)

* **Statuses are separate.** Lifecycle (draft/active/discontinued/archived), approval (pending/approved/rejected) and
  publication (unpublished/published). Only approved + published + active/discontinued products appear in feeds and
  the catalog API. Approving an active product publishes it (the Phase 1 behaviour); you can unpublish without losing
  the approval. Deleting is a soft delete — history is kept and the product can be restored.
* **Data provenance.** Website values (`source_values`), extracted values (evidence), merchant edits
  (`manual_overrides`), AgentReady-managed prices/stock and store imports are tracked separately.
* **Rescans never overwrite merchant-managed values.** If the website changes a field you manage, a *sync conflict*
  records the website value, your value, the previous website value and the evidence; you accept, keep, edit or
  ignore it. Per website you can choose a policy (ask me / keep mine / take the website's), a schedule
  (manual/daily/weekly/monthly, timezone-aware), page/product limits, extraction methods and which changes to detect.
* **Money** uses `NUMERIC(14,2)` and Python `Decimal` (half-up to cents); bulk fixed-amount changes never touch
  products priced in another currency. Every price change writes an append-only price-history row; every stock
  change writes an append-only inventory transaction. Stock updates use optimistic concurrency (`inventory_version`).
* **Integrations** (`/app/integrations`): WooCommerce REST v3 (consumer key/secret, HTTPS only) and Shopify Admin
  GraphQL (OAuth with `SHOPIFY_API_KEY`/`SHOPIFY_API_SECRET`, or a custom-app access token). Credentials are
  Fernet-encrypted, never returned or logged; store HTTP calls go through the same SSRF guard as the crawler; webhooks
  are HMAC-verified and de-duplicated. Imports never overwrite merchant-managed fields (they raise conflicts).
* **Catalog API v2** (`/api/v2/catalog/...`, schema `agentready-catalog/2.0`): products, search, categories,
  variants, availability, pricing, inventory and feeds (full, incremental with tombstones, category, availability).
  API keys have scopes (`catalog:read`, `pricing:read`, `inventory:read`, `evidence:read`), per-key rate limits,
  usage tracking, rotation and revocation. Cost prices, reserved stock and locations are never exposed.
  `/api/v1/catalog` keeps its Phase 1 response shape.

### AI commerce distribution (Phase 3)

* **Off by default, enforced server-side.** Settings → *Distribution → Public products*: master switch, per-channel
  switches (agent API, MCP, feeds, webhooks), all published products or only selected ones, excluded categories
  (with subcategories), shared/withheld prices and availability, public/hidden attributes, an expiry date, and an
  opt-in *public network* that lets anonymous agents discover the merchant. Products can opt out, be limited to
  channels, and carry a public description/images that never replace the internal description or its evidence.
* **One public serializer** (`app/services/distribution/public.py`, schema `agentready-product/1.0`) produces every
  external response: agent API, MCP tools, feeds, webhook payloads and the merchant's preview. It never includes
  cost prices, stock quantities, reserved stock, locations, notes, evidence or credentials. Unknown values are listed
  in `unknown_fields`, values the merchant withheld in `withheld_fields`; `availability.status` `unknown` is distinct
  from `out_of_stock`. Prices keep their currency; price filters/sorting across currencies are refused, not converted.
* **Agent API** `/api/v3/agents`: `search` / `products` (keywords, brand, SKU, GTIN, category, price range, currency,
  availability, merchant, `attr=voltage:230 V`, `attr=storage>=512 GB`, `variant=size:M`), `products/{id}` with
  `/variants`, `/availability`, `/pricing`, `compare`, `recommend`, `categories`, `merchants`, `changes?since=`
  (incremental with tombstones), typed `tools`, carts and checkout hand-offs. Discovery document:
  `/api/v3/agents/capabilities` and `/.well-known/agentready.json`. Authentication: an API key with `agent:read`
  (that merchant), or none (public-network merchants, 60 requests/min per IP).
* **Search** runs behind a `SearchBackend` interface: SQL candidate retrieval (LIKE on text fields and attribute
  values, `pg_trgm` word similarity on PostgreSQL), filters evaluated on the public representation, and a published
  ranking (text, exact identifier, brand, attributes, completeness, availability). No paid placement. Units are
  converted only with explicit rules (`units.py`); incompatible units are never compared.
* **MCP** `/api/v3/mcp` (Streamable HTTP, JSON responses): protocol 2026-07-28 (stateless, `server/discover`) and the
  2025-11-25/2025-06-18/2025-03-26 handshake. Nine read-only tools with input and output schemas; no pricing,
  inventory or publication actions. Verified with the official MCP Python SDK 2.3.0 (both eras), SDK 1.16 and the
  MCP Inspector CLI. Client config: `{"mcpServers": {"agentready": {"type": "http", "url": ".../api/v3/mcp",
  "headers": {"Authorization": "Bearer <agent key>"}}}}`.
* **Feeds** (*Distribution → Product feeds*): full, incremental (content-hash diff against the previous snapshot, with
  removal tombstones), availability, pricing, category and variant feeds; profiles `agentready`, `openai` (OpenAI
  Product Feed field names) and `google` (Merchant Center). Versioned snapshots with validation reports, real
  progress, scheduled regeneration (hourly/daily/weekly, claimed once) and expiry (an expired snapshot is regenerated
  before it is served). Public feed URLs use hashed, revocable, expiring, rate-limited `arf_` tokens; access logs
  (uvicorn and nginx) redact them.
* **Webhooks**: a `before_flush` hook writes every product change into a transactional outbox (core UPDATEs such as
  stock changes call `note_event`), which is fanned out to subscriptions. Deliveries are signed per Standard Webhooks
  (`webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<HMAC-SHA256>`), sent through the SSRF guard (HTTPS,
  public addresses, pinned IP, no redirects), retried 8 times with exponential backoff and jitter, replayable with the
  same `webhook-id`, and both secrets sign for 24 h after rotation. Payloads hold only the public representation or a
  tombstone. The same commit hook invalidates the public response cache (30 s TTL, never past a sale boundary).
* **Checkout foundation**: carts and checkout sessions verify publication, distribution permission, current price,
  currency, availability and quantity (without revealing stock numbers), then hand the shopper to the merchant's own
  checkout (Shopify cart permalink, WooCommerce add-to-cart, or the product page). No payment is processed;
  `payment_status` is always `not_handled`.
* **AI platforms**: see [docs/phase3-platforms.md](docs/phase3-platforms.md). ChatGPT and Google feeds can be
  generated and validated; live submission is BLOCKED pending platform approval/credentials; Perplexity documents no
  merchant API. Statuses never show "connected/active" without evidence.
* **Analytics** (*Distribution → Analytics*): only observed events (requests served, MCP calls, feed downloads and
  generations, webhook deliveries, checkout hand-offs, errors), grouped by consumer. Recommendations, impressions,
  purchases and conversions inside AI platforms are not measured and are not estimated.

### Commerce intelligence (Phase 4)

* **Quality score** = `100 x sum(weight x dimension score) / sum(weights of applicable dimensions)` over 12 dimensions
  (basic info, description, images, identifiers, pricing, category, attributes, variants, freshness, distribution
  readiness, structured-data validity, source provenance). Weights are configurable; dimensions that do not apply are
  excluded; values the merchant withholds from distribution are listed, not penalised. Category-specific
  requirements come from required attribute definitions (and per-category settings). Levels: excellent >= 85,
  good >= 70, needs improvement >= 50, critical < 50. The score measures completeness/validity/freshness/provenance —
  never factual accuracy. Results are stored per product and recomputed incrementally (only products, variants or
  settings that changed, plus a daily refresh for freshness); one snapshot per day gives the trend and week/month
  comparisons (coverage is reported when history is too short).
* **Discovery insights** are computed from the Phase 3 `distribution_events` (search terms, queries, filters and
  attributes requested, zero/few-result queries, per-product detail/pricing/availability/compare requests). They are
  agent interest signals, never purchases or conversions. Zero-result queries are matched against the merchant's own
  catalog to explain *why* nothing was returned (e.g. matching motors lack a voltage attribute) without assuming the
  merchant sells the item.
* **Recommendations** (12 categories) and **anomalies** (13 rules) are deterministic, carry evidence and a published
  priority/severity rule, keep dismissals, auto-resolve when the cause disappears, and never change data. Two
  recommendation actions are automated after explicit confirmation: linking products to a matching managed category,
  and drafting descriptions as *pending* suggestions.
* **AI assistant** (`/app/intelligence/assistant`): questions are routed deterministically, facts are retrieved with
  SQL (scoped, never the whole catalog), and Groq — when configured — only phrases the answer from those facts using
  structured output. A grounding check rejects any answer containing numbers or product references not present in the
  retrieved structured data, falling back to the deterministic answer. Description/attribute suggestions may not add
  numbers or claims (warranty, certification, ...) absent from the source; website text is treated as untrusted data.
  Approved suggestions go through the normal product edit path (validation, merchant override, evidence, audit); a
  suggestion made before a newer merchant edit is refused. Every Groq call is logged in `ai_requests` with tokens and
  estimated cost.
* **Inventory forecasting is intentionally unavailable**: AgentReady receives no orders, and manual adjustments are not
  demand. Prices are never compared across currencies.
* **Reports** (10 kinds) include period, merchant, generation time, definitions, data coverage and missing-data
  warnings; unavailable metrics are `null`, never 0. **Notifications** (10 alert types) are grouped, thresholded,
  can be disabled and are in-app only — no email provider is configured, so `email_status` is `not_configured`.
* Cross-merchant benchmarking is deferred: it cannot be offered without consent and a privacy-preserving design.

Walkthrough (19 steps): `AR_WEB=http://localhost:5173 python eval/ui_walkthrough_phase4.py` (or `:8080` for Docker).

### Groq configuration

AgentReady calls `POST {GROQ_BASE_URL}/chat/completions` directly with `httpx` (no vendor SDK).

| Variable | Default | Notes |
|---|---|---|
| `AI_PROVIDER` | `groq` | `groq`, `mock` (clearly labelled local mock for tests/demos), or `none` |
| `GROQ_API_KEY` | — | Without it, scans run deterministic extraction only and say so in the scan log |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Checked against `GET /models` before the first call; use **Settings → AI usage → Verify model** |
| `GROQ_RESPONSE_FORMAT` | `auto` | `json_schema` (strict for gpt-oss models) where supported, else JSON mode; falls back automatically if the API rejects the schema |
| `GROQ_INPUT/OUTPUT_PRICE_PER_MTOK` | 0.15 / 0.60 | For cost **estimates** only; confirm against Groq's current pricing |
| `AI_EXTRACTION_MODE` | `fallback` | `fallback`, `always`, `never` |
| `MAX_AI_CALLS_PER_SCAN`, `MAX_AI_CONTENT_CHARS` | 60, 12000 | Cost controls |

Requests are logged to `ai_requests` with tokens, latency, attempts, response format and estimated cost; identical
content is served from a per-merchant cache. 429/498/5xx and timeouts are retried with backoff (honouring
`retry-after`).

### Security

Summary: SSRF-guarded crawler and outbound calls, bcrypt passwords, short-lived revocable sessions, per-IP and
per-account login throttling, hashed scoped API keys and feed tokens, encrypted integration credentials and
webhook secrets, tenant isolation on every query, one public serializer for every external surface, CSP and
security headers, request size limits, secret redaction in logs, and production configuration that refuses unsafe
settings. Details and test references: [SECURITY.md](SECURITY.md).

### API

OpenAPI docs at `/docs` (conventions, v2/v3, MCP and health: [API_DOCUMENTATION.md](API_DOCUMENTATION.md)). Main endpoints (all under `/api/v1`):

`auth/register · login · logout · me · forgot-password · reset-password · change-password` ·
`websites (+ validate-url)` · `scans · scans/{id} · /progress · /cancel · /changes · /apply · /pages` ·
`products · products/{id} (GET/PATCH/DELETE) · products/categories · products/bulk-approval` ·
`validation/issues · validation/summary · validation/issues/{id}/resolve` ·
`exports · exports/{id}/download` · `catalog · catalog/{id}` (approved products only; session token or
`X-Catalog-Key`) · `dashboard/overview` · `business · business/catalog-key · settings/ai · settings/ai/verify`.

Errors use one envelope: `{"error": {"code", "message", "details"}}`.

---

## Tests

```bash
cd backend
pytest                                   # SQLite (fast)
# PostgreSQL (create the agentready_test database first; port is POSTGRES_HOST_PORT, default 5432):
TEST_DATABASE_URL=postgresql+asyncpg://agentready:agentready@localhost:5432/agentready_test pytest
```

The suite (170 tests) covers URL/SSRF protection, normalization, table/HTML/JSON-LD extraction, page classification,
the Groq HTTP contract (against a simulated API), AI hallucination guards, authentication, merchant data isolation,
exports, scan concurrency/recovery, hostile-page limits, and a full end-to-end merchant journey including rescanning
and change review. Phase 2 tests (`test_commerce.py`, `test_integrations.py`) cover product CRUD and lifecycle, mass
assignment, pricing rules and Decimal rounding, immutable price history, scheduled price changes (applied exactly
once), inventory operations and **concurrent stock updates without lost updates**, low-stock detection, variants,
categories, attribute definitions/aliases, bulk preview/partial failure/background runs, timezone/DST-aware schedules,
scheduled rescans, conflict detection and resolution, the v2 catalog API (scopes, feeds, tombstones, rate limits,
rotation, revocation, usage), tenant isolation, audit logging, and integrations. Fixture websites live in
`backend/app/tests/fixtures/` and are served from memory — the automated tests never touch the network or the real
Groq API. **WooCommerce and Shopify tests run against mocked APIs; they are not live provider verification.**
Phase 5 tests (`test_production.py`) cover production configuration validation, real health checks (including a
database outage → 503), request size limits, security headers, request IDs, log redaction, per-account login
throttling, reset-link invalidation, interrupted-job recovery, scheduler stage isolation, concurrent intelligence
ticks, the daily notification frequency, the System status endpoint and private data on every public surface.

Frontend: `npm run typecheck`, `npm run build`, `npm test` (Vitest unit tests).

Performance: `PERF_DATABASE_URL=postgresql+asyncpg://agentready:agentready@localhost:55432/agentready_perf python
eval/perf_benchmark.py --products 5000` (creates and fills a disposable database; refuses names without "perf").

### Live checks (not part of `pytest`)

```bash
cd backend
python -m eval.run_eval --provider groq      # full pipeline with REAL Groq calls (needs GROQ_API_KEY)
```

The UI was verified with a Playwright script that drives the real app through the full merchant journey on
desktop and a 390 px mobile viewport (see "Verifying a deployment" below).

## Configuring and verifying Groq

1. Put the key in `backend/.env` (local dev) and/or the root `.env` (docker compose):
   `GROQ_API_KEY=gsk_...` — never commit these files (both are git-ignored).
2. Restart the API (and worker).
3. Check `GET /health` → `"ai_configured": true`.
4. In the app: **Settings → AI usage → Verify model**. This calls Groq's `GET /models` and confirms `GROQ_MODEL`
   is available to your key (a 401 means the key is wrong).
5. Run `python -m eval.run_eval --provider groq` from `backend/`. It prints the model, request count, tokens and
   estimated cost, and writes `eval/results/groq.json`.
6. Scans that used AI show it on the scan page ("AI usage in this scan"), and AI-derived values are labelled
   `AI · <model>` in each product's Evidence tab.

## Measured extraction quality

`backend/eval/` contains a manually verified ground-truth dataset for the fixture shop and a harness that measures
discovery precision/recall, product recall, field/price/attribute accuracy, duplicate detection, completeness, human
review rate, processing time and AI tokens per 100 products:

```bash
cd backend
python -m eval.run_eval                  # deterministic only
python -m eval.run_eval --provider mock  # exercises the AI code path with the LOCAL MOCK (not a real model)
python -m eval.run_eval --provider groq  # real Groq calls (requires GROQ_API_KEY)
```

The dataset is one small synthetic site (10 products, 11 pages). Its numbers show the pipeline behaves as designed on
known cases; **they are not real-world accuracy figures** and must not be quoted as such. Build a larger verified
dataset from real merchant sites before claiming accuracy.

## Verifying a deployment

```bash
cd backend
pip install -r requirements-playwright.txt && python -m playwright install chromium
AR_WEB=http://localhost:8080 python eval/ui_walkthrough.py
```

Prints PASS/FAIL per step of the merchant journey plus any console errors and unexpected API failures, and saves
desktop/mobile screenshots to `backend/eval/results/screenshots/`.

Phase 5 production walkthrough (every sidebar page at desktop and 390 px width with console/CSP-error, 5xx and
overflow checks; System status; session expiry; security headers):

```bash
AR_WEB=http://localhost:8080 python eval/ui_walkthrough_phase5.py
```

Phase 2 commerce walkthrough (17 steps: manual product, pricing, inventory, variants, categories, bulk updates,
scheduled rescan, website change review, conflict resolution, feed, API key, audit log, mobile):

```bash
# it serves a fixture shop on port 8765 so it can change the "website" between scans; the API must allow that
# DEV-ONLY destination: ALLOW_PRIVATE_NETWORKS=true ALLOWED_PORTS=80,443,8765
AR_WEB=http://localhost:5173 python eval/ui_walkthrough_phase2.py
# docker stack (compose reads these overrides from the shell; defaults stay safe):
#   ALLOW_PRIVATE_NETWORKS=true ALLOWED_PORTS=80,443,8765 docker compose up -d
AR_WEB=http://localhost:8080 AR_SHOP=http://host.docker.internal:8765 python eval/ui_walkthrough_phase2.py
```

Phase 3 distribution walkthrough (18 steps: distribution settings, product selection, preview, JSON and JSON-LD
feeds, feed URL, agent API, filtered search, comparison, availability, pricing, a real MCP client, webhook creation,
a product update, signed webhook delivery, analytics; plus platform validation and a 390 px check):

```bash
# a local webhook receiver listens on 8766 -> DEV-ONLY: ALLOW_PRIVATE_NETWORKS=true ALLOWED_PORTS=80,443,8766
# step 14 runs the official MCP Python SDK from a separate virtualenv (see requirements-mcp-client.txt)
python -m venv .venv-mcp && .venv-mcp/Scripts/pip install -r requirements-mcp-client.txt
AR_WEB=http://localhost:5173 AR_MCP_PY=.venv-mcp/Scripts/python python eval/ui_walkthrough_phase3.py
AR_WEB=http://localhost:8080 AR_HOOK=http://host.docker.internal:8766 AR_MCP_PY=.venv-mcp/Scripts/python \
  python eval/ui_walkthrough_phase3.py
# MCP check on its own (any running stack with an agent:read key):
AR_MCP_URL=http://localhost:8000/api/v3/mcp AR_KEY=ar_cat_... .venv-mcp/Scripts/python eval/mcp_client_check.py
```

## Project structure

```
agentready/
  backend/
    app/
      api/v1/          REST routers
      core/            config, security, errors, rate limiting
      db/              engine/session, portable types (UUID, JSONB)
      models/          SQLAlchemy models
      schemas/         API schemas + canonical product schema
      services/        crawler · discovery · extraction · ai · normalization · validation · deduplication · catalog · export
      workers/         scan orchestration, Celery app, dispatch
      tests/           pytest suite + fixture websites
    alembic/           migrations
    eval/              ground truth + evaluation harness
  frontend/src/        components · pages · layouts · hooks · services · types · utils · styles
  frontend/nginx/      nginx snippets (security headers/CSP, proxy) and the production TLS server
  docs/                feed format, AI-platform research
  scripts/             backup.sh / restore.sh (+ PowerShell versions)
  deploy/render/       single-container image for Render (API + built SPA)
  docker-compose.yml  docker-compose.prod.yml  render.yaml  .env.example
  *.md                 guides listed under "Documentation"
```

## Documentation

| Document | For |
|---|---|
| [MERCHANT_USER_GUIDE.md](MERCHANT_USER_GUIDE.md) | merchants: every module in plain language |
| [INSTALLATION.md](INSTALLATION.md) | running it locally (Docker or dev servers) |
| [DEPLOYMENT.md](DEPLOYMENT.md) | production: configuration, TLS, migrations, backups, restore, rollback, health checks |
| [ARCHITECTURE.md](ARCHITECTURE.md) | components, data flow, design rules |
| [API_DOCUMENTATION.md](API_DOCUMENTATION.md) | API conventions and endpoints (full reference at `/docs`) |
| [MCP_INTEGRATION.md](MCP_INTEGRATION.md) | connecting MCP clients |
| [SECURITY.md](SECURITY.md) | security controls and how they are verified |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | symptoms → causes → fixes |
| [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) | what is tested, what needs credentials/approval, what is not built; measured performance |
| [docs/feed-format.md](docs/feed-format.md), [docs/phase3-platforms.md](docs/phase3-platforms.md) | feed format; AI-platform research |
| [docs/PHASE5_ACCEPTANCE_REPORT.md](docs/PHASE5_ACCEPTANCE_REPORT.md) | final verification results (PASS / NOT TESTED / BLOCKED) |

## Known limitations

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for the full list, including which integrations are tested, which
need credentials, which are blocked by third-party approval, and the measured performance limits.

## Author

**Vedula Uday Easwar**: design, architecture and implementation of AgentReady.

* GitHub: [@Vedulaudayeaswar](https://github.com/Vedulaudayeaswar)
* Email: vedulaudayeaswar2004@gmail.com

© 2026 Vedula Uday Easwar. All rights reserved.
