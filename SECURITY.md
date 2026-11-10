# Security

This document lists the security controls in AgentReady, how each is verified, and the known gaps. Report a
vulnerability privately to the maintainers; do not open a public issue.

## Authentication and sessions

| Control | Implementation | Verified by |
|---|---|---|
| Password hashing | bcrypt (cost 12) over a SHA-256 pre-hash (no 72-byte truncation) | `test_auth.py::test_password_hash_is_not_plaintext` |
| Password policy | 10+ characters (registration, reset, change) | `test_auth.py::test_bad_credentials_and_validation` |
| Sessions | HS256 JWT, 30 min (`ACCESS_TOKEN_EXPIRE_MINUTES`), `jti` revocation on logout, invalidated on password change/reset | `test_auth.py`, `test_forgot_and_reset_password` |
| Token storage (browser) | `sessionStorage` (cleared when the tab closes); bearer header, so no cookies → no CSRF surface | design |
| Login enumeration | identical response and timing (dummy hash) for unknown emails | `test_auth.py` |
| Brute force | per-IP limit on auth endpoints (10/min) **and** per-account failed-login throttle (10 per 15 min, counted for unknown emails too); cleared by a successful login or password reset | `test_login_rate_limit`, `test_production.py::test_failed_logins_are_throttled_per_account` |
| Password reset | random 256-bit token, stored as SHA-256, single use, 30 min expiry; using one link invalidates every other outstanding link; link only returned/logged in `ENVIRONMENT=development` | `test_hardening.py::test_reset_token_never_logged_outside_development`, `test_password_reset_invalidates_other_reset_links_and_clears_throttle` |
| Real client IP | nginx overwrites `X-Forwarded-For` with `$remote_addr`; uvicorn trusts it only from `FORWARDED_ALLOW_IPS`; the API port is not published in production | Docker check: spoofed `X-Forwarded-For` does not bypass the limit |

## Authorization and tenant isolation

* Every merchant endpoint depends on `current_auth`; every query is scoped to the authenticated merchant's
  `business_id`. Isolation tests exist for each phase (`test_merchant_data_isolation`, `test_phase2_tenant_isolation`,
  `test_merchant_isolation_and_public_network`, `test_intelligence_is_tenant_isolated`, system status isolation).
* Request schemas use `extra="forbid"`: clients cannot mass-assign internal fields (`test_mass_assignment_and_validation_rejected`).

## API keys and feed tokens

* Catalog/agent keys (`ar_cat_…`) and feed tokens (`arf_…`) are random 256-bit values, stored only as SHA-256
  hashes, shown once, with scopes (`catalog:read`, `pricing:read`, `inventory:read`, `evidence:read`,
  `agent:read`, `checkout:write`), per-key rate limits, usage counters, expiry, rotation and revocation.
* Feed tokens travel in the URL path; nginx and the API redact them from access logs.
* Webhook secrets (`whsec_…`) are Fernet-encrypted at rest; integration credentials (WooCommerce/Shopify) are
  Fernet-encrypted with `CREDENTIALS_ENCRYPTION_KEY` and never returned to the browser or logged.

## Public data exposure

The only external representations are produced by the public serializer (`agentready-product/1.0`) and the
scoped v1/v2 catalog APIs. They never include cost prices, stock quantities (agent API/MCP/feeds), reserved stock,
locations, internal notes, evidence, approval internals, credentials, or private attribute rows (`supplier`,
`cost`, `cost_price`, `margin`, `notes`, `internal_notes`, `stock_quantity`). Distribution is **off by default**
and enforced server-side; unapproved/unpublished products return the same 404 as missing ones.
`test_production.py::test_private_merchant_data_never_reaches_public_surfaces` plants sentinel values and checks
the agent API (search, detail, pricing, availability), MCP tools, every feed format through a real feed URL, the v1
and v2 catalog APIs and the discovery document. *(Phase 5 found and fixed one leak: the v1/v2 catalog APIs returned
private attribute rows such as `Supplier`.)*

## Crawler and outbound requests (SSRF)

http/https only, port allow-list, own DNS resolution with every address checked (loopback, private, link-local and
cloud metadata, CGNAT, multicast, reserved, IPv4-mapped IPv6), the connection pinned to the validated IP (Host
header and TLS SNI preserved) against DNS rebinding, every redirect re-validated (max 5), streamed size limit
(5 MB), text content types only, no cookies kept, robots.txt respected. The same guard is used for WooCommerce and
Shopify calls and for webhook deliveries (which additionally require HTTPS and do not follow redirects).
`ALLOW_PRIVATE_NETWORKS=true` exists only for local test fixtures and **is refused at startup in production**.
Tests: `test_ssrf.py`, `test_e2e.py::test_ssrf_urls_rejected_at_api`.

## Input handling

* SQL: SQLAlchemy parameterised queries only; `LIKE` wildcards in user search are escaped (`test_search_treats_wildcards_literally`).
* XSS: React escapes all rendered text; no `dangerouslySetInnerHTML`; CSP forbids inline and third-party scripts.
* Request bodies: limited to 1 MB at nginx and `MAX_REQUEST_BODY_BYTES` (2 MB) in the API, including chunked
  bodies (413). There are no file-upload endpoints; catalog imports come from websites and store APIs.
* Exports: CSV formula injection neutralised; downloads are attachments with `nosniff`.
* Hostile pages: element-count/depth limits, AI content limits, prompt-injection delimiters neutralised.

## HTTP headers

* SPA (nginx): `Content-Security-Policy` (`script-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`,
  `base-uri 'self'`, `form-action 'self'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and
  `Strict-Transport-Security` over HTTPS.
* API: the same frame/nosniff/referrer/permissions headers, `Cache-Control: no-store` on authenticated and agent
  responses, `X-Request-ID`, `Server-Timing`.
* CORS: explicit origins only (`CORS_ORIGINS`; `*` is refused in production), no credentials.

## Secrets and configuration

* Secrets come only from the environment (`.env`, git-ignored). `.env.example` contains placeholders only.
* With `ENVIRONMENT=production` the API refuses to start unless: `SECRET_KEY` ≥ 32 characters and not the default,
  a valid Fernet `CREDENTIALS_ENCRYPTION_KEY`, `ALLOW_PRIVATE_NETWORKS=false`, `AI_PROVIDER` not `mock`, explicit
  `CORS_ORIGINS`, `JOB_BACKEND=celery`, PostgreSQL (`test_production_configuration_is_validated`).
* Interactive API docs (`/docs`) are disabled in production unless `API_DOCS_ENABLED=true`.
* The Groq key is used only server-side; it never reaches the browser bundle.

## Logging

Every log record passes a redaction filter that removes feed tokens, catalog keys, webhook secrets, Groq keys,
bearer tokens and secret query parameters (`token=`, `key=`, …) before formatting
(`test_logs_redact_secrets_and_json_format`). Passwords are never logged. Audit log entries replace values under
credential-like keys. Errors return a request ID, never stack traces or SQL.

## Known gaps (not implemented)

* No multi-factor authentication and no account lockout notification e-mails (no e-mail provider is configured;
  password-reset links are not delivered outside development — see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)).
* Sessions are 30-minute bearer tokens without refresh; the user signs in again after expiry.
* `style-src 'unsafe-inline'` is needed by the chart library's inline styles.
* Rate limits fall back to per-process memory if Redis is unreachable (and always with `JOB_BACKEND=inline`).
* No automated dependency/container vulnerability scanning is configured (run `pip-audit` / `npm audit` / an image
  scanner in your CI).
