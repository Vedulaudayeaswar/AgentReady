# Known limitations

Status vocabulary used across the documentation:

* **Implemented and tested** — works and is covered by automated tests and/or browser walkthroughs.
* **Implemented, needs external credentials** — code exists and is tested against simulated services; it needs
  the merchant's/operator's own credentials to work for real.
* **Requires third-party approval** — cannot be completed without a platform's approval programme.
* **Not implemented** — deliberately out of scope so far.

## Integration status

| Capability | Status |
|---|---|
| Website scanning, extraction, evidence, approval, commerce management, agent API, MCP, feeds, webhooks (to any HTTPS receiver), checkout hand-off URLs, intelligence, reports, notifications (in-app) | Implemented and tested |
| Groq AI (extraction fallback, assistant, description suggestions) | Implemented and tested **live** with `openai/gpt-oss-120b` (needs `GROQ_API_KEY`) |
| Official MCP SDK clients (Python SDK 2.3.0 both protocol eras, SDK 1.16, MCP Inspector) | Implemented and tested live against the running stack |
| WooCommerce import/push/webhooks | Implemented, needs external credentials (tested against a simulated store only) |
| Shopify import (token or OAuth)/push/webhooks, Shopify cart permalinks | Implemented, needs external credentials (OAuth needs a Shopify app; tested against mocked APIs) |
| OpenAI product feed (field names, validation) | Implemented and tested; **submission to ChatGPT requires third-party approval** (SFTP credentials from OpenAI) — BLOCKED |
| Google Merchant Center feed (RSS/`g:` and CSV) | Implemented and tested; Merchant API submission and UCP checkout **require third-party approval** — BLOCKED |
| Perplexity | No documented merchant API — BLOCKED, nothing is submitted |
| Being used by ChatGPT, Gemini, Perplexity or other hosted assistants | Not claimed: an API/feed/MCP endpoint existing does not mean a platform consumes it |
| E-mail delivery (password reset, notification e-mails) | Not implemented: no e-mail provider; `email_status` is `not_configured` |
| Payments, orders, revenue, conversion tracking | Not implemented (AgentReady hands shoppers to the merchant's checkout and never sees orders) |
| Multi-factor authentication, SSO, multiple users per business, roles | Not implemented |
| Cross-merchant benchmarking | Not implemented (needs consent and a privacy-preserving design) |
| Inventory demand forecasting | Not implemented by design (no order data; manual adjustments are not demand) |
| PDF/spreadsheet/image catalog ingestion | Not implemented |

## Operations

* Password resets in production: with no e-mail provider the reset link is not delivered (the API logs that a
  reset was requested, without the token). An operator must help the user until a provider is added.
* Single-server Compose deployment. There is no Kubernetes/Helm configuration, no managed-database setup and no
  automated off-site backup shipping (the scripts write local files; copy them off the server yourself).
* No CI pipeline is configured in this repository; tests and walkthroughs are run manually.
* No metrics endpoint (Prometheus) or tracing; observability is structured logs, request IDs, `Server-Timing`,
  `/health` and the System status page.
* The worker health check pings over the broker; a worker that is alive but stuck inside one task still answers.
  Task time limits (55 min soft / 60 min hard) bound such tasks.
* Rate limits and the public response cache use Redis with Celery; with `JOB_BACKEND=inline` (development) they
  are per process.
* JavaScript-rendered shops need the optional Playwright build (`WITH_PLAYWRIGHT=1`), which has no automated test.
* Progress updates use polling (1.5 s), not WebSockets. Sessions are 30-minute tokens without refresh.

## Performance and scaling

Measured in Phase 5 with `backend/eval/perf_benchmark.py` against PostgreSQL 16 in Docker Desktop on a Windows
laptop, 5,000 synthetic products with 2 attributes each, requests sent in-process to the real ASGI app (no nginx),
15 runs per endpoint after a warm-up. These are indicative numbers for this machine, not a capacity guarantee.

| Operation (5,000 products) | p50 | p95 |
|---|---|---|
| MCP `search_products` | 38 ms | 48 ms |
| Agent search `q=motor` / with attribute filter | 78 / 56 ms | 94 / 78 ms |
| Agent product detail | 74 ms | 82 ms |
| Merchant catalog list, page 1 / page 60 (50 per page) | 97 / 119 ms | 127 / 162 ms |
| Merchant catalog search | 140 ms | 155 ms |
| Dashboard overview / charts | 160 / 141 ms | 175 / 222 ms |
| System status | 59 ms | 106 ms |
| Intelligence overview | 2.06 s | 2.29 s |
| Catalog quality list | 1.94 s | 2.07 s |

| One-off operation (5,000 products) | Time |
|---|---|
| Full feed generation (JSON) | 6.0 s |
| Catalog-quality PDF report | 9.8 s |
| Intelligence pass, all products re-scored | 20.8 s |
| Intelligence pass, nothing changed (quality re-scoring itself takes ~40 ms; the rest is anomalies, recommendations and alerts, which scan the catalog) | 17.8 s |

The machine was noticeably slower during this final run than during the first Phase 5 run (seeding took 64 s vs
35 s; catalog list 97 ms vs 54 ms), so treat the numbers as a ±2x band. Background passes run in the worker, not in
merchant requests.

Known scaling limits:

* **Intelligence pages are O(catalog size).** The overview, catalog-quality list, recommendations and alert
  evaluation load every quality result of the merchant per request/pass (most of the time is transferring ~1.2 KB
  of per-product dimension details). Phase 5 stopped eager-loading attributes/variants there (the shared result load went from 1.8 s to
  1.2 s in isolation; end-to-end page times stayed ~2 s in the noisier final run), and at 50,000 products these pages would take ~10–20 s. Next step: SQL-side aggregation / a materialized summary.
* **Agent search** ranks up to 2,000 SQL candidates per query in the application (responses say when the candidate
  set was truncated). Replace the `SearchBackend` with a dedicated search engine for very large catalogs.
* Feed snapshots and reports are stored in PostgreSQL (feed snapshots up to 50 MB each); exports are text columns.
  Move them to object storage for large catalogs.
* The scheduler evaluates up to 20 merchants' intelligence per tick (each at most every 10 minutes); many large
  merchants need more workers.
* Product listings use offset pagination; very deep pages of very large catalogs get slower.

## Data and AI

* Measured extraction quality comes from a small synthetic fixture site (10 products). It shows the pipeline works
  as designed; it is **not** a real-world accuracy figure.
* Exchange rates are never applied; each product keeps its own currency and cross-currency price filters are refused.
* The quality score measures completeness, validity, freshness and provenance — not factual accuracy.
* Discovery insights from anonymous public-network searches that return nothing cannot be attributed to a merchant.
* Webhooks are not emitted for sale windows that start/end without a database change (served prices are still
  correct at request time).
* Reserved stock is entered manually (no order processing).
