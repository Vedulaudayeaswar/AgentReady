# Phase 5 acceptance report — production readiness

Date: 2026-11-09. All results below come from runs in this phase. Status: **PASS** = implemented and verified,
**FAIL** = not working, **NOT TESTED** = not verified, **BLOCKED** = needs external credentials/approval/infrastructure.

## 1. Audit findings (Milestone 0)

Before Phase 5: 156/156 backend tests passing; Docker stack healthy. Gaps found by reading the code:

| # | Finding | Severity | Resolution |
|---|---|---|---|
| 1 | Behind nginx every request had nginx's IP → one shared per-IP rate-limit bucket for all users; no per-account login throttle | High | nginx overwrites `X-Forwarded-For`; uvicorn trusts it via `FORWARDED_ALLOW_IPS`; per-account throttle added |
| 2 | v1/v2 catalog APIs returned private attribute rows (e.g. `Supplier`) — **found by the new leakage test** | High | private attribute keys filtered on all external APIs |
| 3 | `/health` always returned `ok` | High | real DB / Redis / worker / scheduler checks; 503 when critical deps are down |
| 4 | Feed snapshots, bulk operations, integration syncs stayed "running" forever after a worker crash; stale scans only recovered when the same merchant started a new scan | High | `workers/recovery.py`, run every scheduler tick |
| 5 | One failing scheduler stage skipped all later stages | Medium | each stage isolated |
| 6 | "Daily" notification frequency never notified again after the first pass (Phase 4 bug) | Medium | `last_notified_at` (migration 0006) |
| 7 | Concurrent creation of intelligence settings / distribution profile → IntegrityError; overlapping ticks evaluated a merchant twice | Medium | savepoint get-or-create; conditional claim |
| 8 | Production check refused only the default SECRET_KEY | Medium | full production validation (8 rules) |
| 9 | No body-size limit on the API, no CSP/Permissions-Policy, 500s without request id, unstructured logs, no latency logging | Medium | `RequestContextMiddleware`, CSP in nginx, JSON logs |
| 10 | Other reset links stayed valid after a password reset | Low | invalidated |
| 11 | No production compose/TLS, restart policies, DB wait on startup, backup/restore | Medium | prod overlay, entrypoints, scripts |
| 12 | All pages + charts (≈1.28 MB JS) downloaded on first visit; Settings tabs overflowed at 390 px | Low | route-level code splitting; scrollable tab bar |

## 2. Results by requirement

| Requirement | Status | Evidence |
|---|---|---|
| Auth: hashing, sessions, logout, reset, per-IP + per-account throttling | PASS | `test_auth.py`, `test_production.py` (throttle, reset invalidation); Docker: spoofed XFF still limited (10×401 then 429) |
| Authorization / merchant isolation | PASS | isolation tests in every phase + system status isolation |
| API keys (hashing, scopes, rotation, revocation, rate limits) | PASS | `test_catalog_v2_scopes_feeds_and_keys`, `test_feed_tokens_*` |
| SSRF / URL validation / redirects | PASS | `test_ssrf.py`, `test_ssrf_urls_rejected_at_api` |
| File uploads | PASS (N/A) | no upload endpoints exist; body limit 413 tested incl. chunked bodies |
| SQL injection / XSS | PASS | parameterised queries, wildcard escaping test; React escaping + CSP without inline scripts (walkthrough: 0 CSP errors on 33 pages) |
| CORS / CSRF / security headers / cookies | PASS | explicit origins (`*` refused in prod); bearer tokens, no cookies; headers asserted in tests and on Docker + prod TLS stack (HSTS) |
| Secret management + env validation | PASS | `test_production_configuration_is_validated`; prod container refused `SECRET_KEY=short` + `ALLOW_PRIVATE_NETWORKS=true` |
| No secrets in logs | PASS | redaction test; prod stack: 0 reset tokens in API logs; secrets scan of untracked files clean (only fake test samples) |
| No private data via agent API / MCP / feeds / v1 / v2 | PASS (after fix) | `test_private_merchant_data_never_reaches_public_surfaces` |
| Migrations safe (0006 additive, downgrade works, no drift) | PASS | upgrade/downgrade/upgrade on SQLite; applied to live Docker DB; `alembic check` clean |
| Concurrency (inventory, scheduled jobs, intelligence ticks) | PASS | `test_inventory_concurrency_no_lost_updates`, `test_scheduled_*_runs_once`, `test_concurrent_intelligence_ticks_*` |
| Backup and restore | PASS | `scripts/backup.sh` → 974 KB dump, 55 tables verified; restore into `agentready_restore`: counts identical to live (15 / 392 / 220, rev 0005); ORM + `alembic current` read it; copy dropped |
| DB unavailable handling | PASS | `test_database_outage_returns_503_not_500`; entrypoint waits with backoff |
| Job recovery, retries, duplicates, failure visibility | PASS | `test_interrupted_jobs_*`, `test_failing_scheduler_stage_*`; System status page |
| Worker/scheduler restarts | PASS | stack recreated several times; worker health check + scheduler heartbeat report real state |
| Full merchant journey (Phases 1–4 regression) | PASS | Docker walkthroughs: Phase 1 19/19, Phase 2 17/17, Phase 3 20/20 (official MCP SDK 2.3.0, both eras), Phase 4 20/20 (real Groq, grounded) |
| Frontend production quality (all pages, mobile, errors, session expiry) | PASS | Phase 5 walkthrough 6/6: 33 pages desktop + 390 px, 0 console/CSP errors, 0 5xx, 0 overflow |
| Performance measured, limits documented | PASS | `eval/perf_benchmark.py` at 5,000 products; table in KNOWN_LIMITATIONS.md. Intelligence pages ~2 s at 5k products remain a documented limit |
| Production deployment (prod overlay, TLS, health, restart, network exposure) | PASS | booted as a separate project: migrate exit 0, all healthy, only 80/443 published, HTTP→HTTPS 301, HSTS, `/docs` 404, JSON logs, registration 201 |
| Observability (structured logs, request ids, health, latency) | PASS | tests + prod stack log lines with `request_id`/`duration_ms`; `Server-Timing` |
| Documentation (10 documents) | PASS | README, ARCHITECTURE, INSTALLATION, DEPLOYMENT, SECURITY, API_DOCUMENTATION, MCP_INTEGRATION, MERCHANT_USER_GUIDE, TROUBLESHOOTING, KNOWN_LIMITATIONS |
| Real TLS certificate issuance (Let's Encrypt) | NOT TESTED | needs a public domain; tested with a self-signed certificate |
| Load testing with concurrent users | NOT TESTED | only single-client latency was measured |
| WooCommerce / Shopify live stores | BLOCKED | needs merchant credentials (mock-tested) |
| ChatGPT / Google Merchant API / UCP submission | BLOCKED | needs platform approval and credentials |
| Perplexity | BLOCKED | no documented merchant API |
| E-mail delivery | BLOCKED | no e-mail provider configured |

## 3. Test results

| Suite | Result |
|---|---|
| Backend, SQLite | **170 passed** (156 previous + 14 new in `test_production.py`) |
| Backend, PostgreSQL 16 (`agentready_test`) | **170 passed** |
| Frontend type check | pass |
| Frontend unit tests (Vitest) | **15 passed** (13 + 2) |
| Frontend production build | pass; initial JS ≈535 KB raw / ≈160 KB gzip (was ≈1.28 MB / ≈365 KB) |
| Browser walkthroughs on Docker | Phase 1 19/19 · Phase 2 17/17 · Phase 3 20/20 · Phase 4 20/20 · Phase 5 6/6 |

## 4. Files changed in Phase 5

Backend: `core/config.py`, `core/errors.py`, `core/ratelimit.py`, `core/observability.py` (new), `core/health.py`
(new), `main.py`, `db/session.py`, `db/wait.py` (new), `api/v1/auth.py`, `api/v1/system.py` (new),
`api/v1/catalog.py`, `api/v2/catalog.py`, `api/v3/agents.py`, `services/export/exporters.py`,
`services/distribution/public.py`, `services/distribution/profile.py`, `services/intelligence/engine.py`,
`services/intelligence/assistant.py`, `services/intelligence/notifications.py`, `models/intelligence.py`,
`workers/celery_app.py`, `workers/scheduler.py`, `workers/recovery.py` (new), `alembic/versions/0006_production_readiness.py` (new),
`tests/test_production.py` (new), `eval/perf_benchmark.py` (new), `eval/ui_walkthrough_phase5.py` (new),
`Dockerfile`, `docker/start-api.sh`, `docker/start-worker.sh` (new).
Frontend: `App.tsx` (lazy routes), `layouts/AppLayout.tsx`, `pages/app/SystemStatus.tsx` (new),
`components/ui/overlays.tsx`, `components/app/system.test.ts` (new), `vite.config.ts`, `index.html`,
`public/theme-init.js` (new), `nginx.conf`, `nginx/*.conf` (new), `Dockerfile`.
Root: `docker-compose.yml`, `docker-compose.prod.yml` (new), `.env.example`, `.gitignore`, `.gitattributes` (new),
`scripts/backup|restore.sh|ps1` (new), 10 documents. No git commits were made.

## 5. Remaining risks

* Intelligence pages and passes scale linearly with catalog size (~2 s per page at 5,000 products).
* No e-mail provider: password resets in production need operator help.
* No CI, metrics endpoint, MFA, or automated off-site backups.
* Real-world extraction accuracy is unmeasured beyond the synthetic fixture site.
