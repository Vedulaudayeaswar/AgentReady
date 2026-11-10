# API documentation

AgentReady exposes four API surfaces. The interactive OpenAPI reference (every endpoint, every schema) is at
`/docs` and `/api/v1/openapi.json` when API docs are enabled (default outside production; `API_DOCS_ENABLED=true`
turns them on in production). This document explains the conventions and the main endpoints.

| Surface | Base path | Who calls it | Authentication |
|---|---|---|---|
| Merchant API | `/api/v1` | the AgentReady web app | `Authorization: Bearer <session token>` |
| Catalog API (scoped) | `/api/v2/catalog`, `/api/v1/catalog` | merchant systems and partners | `X-Catalog-Key: ar_cat_…` (or a session token) |
| Agent API | `/api/v3/agents` | AI agents, shopping assistants | `X-Catalog-Key` / `Authorization: Bearer` with an `agent:read` key, or none for public-network merchants |
| MCP server | `/api/v3/mcp` | MCP clients | same as the agent API — see [MCP_INTEGRATION.md](MCP_INTEGRATION.md) |
| Feed URLs | `/api/v3/feeds/{token}` | feed readers | the `arf_…` token in the path |
| Health | `/health`, `/health/live` | load balancers, monitoring | none |

## Conventions

**Errors** always use one envelope:

```json
{"error": {"code": "validation_error", "message": "Invalid request", "details": {"errors": [{"loc": ["body", "email"], "msg": "...", "type": "..."}]}}}
```

| Status | `code` examples | Meaning |
|---|---|---|
| 400 | `invalid_token`, `invalid_password` | request understood but not acceptable |
| 401 | `unauthorized`, `invalid_credentials` | missing/expired/revoked session or key |
| 403 | `forbidden` (e.g. key lacks a scope), `distribution_disabled` | authenticated but not allowed |
| 404 | `not_found` | missing **or not visible to you** (other merchant, unpublished product) |
| 409 | `email_taken`, `inventory_conflict`, `scan_in_progress`, `product_changed` | state conflict; re-read and retry |
| 413 | `payload_too_large` | body over 1 MB (nginx) / `MAX_REQUEST_BODY_BYTES` (API) |
| 422 | `validation_error`, `pricing_rule`, `inventory_rule` | invalid input or business rule |
| 429 | `rate_limited`, `too_many_failed_logins` | slow down; honour `Retry-After` |
| 500 | `internal_error` | bug; `details.request_id` identifies the server log entry |
| 503 | `service_unavailable` | database temporarily unreachable; retry after `Retry-After` seconds |

**Request IDs.** Send `X-Request-ID` (8–64 chars `[A-Za-z0-9._-]`) to correlate logs; otherwise one is generated.
Every response returns it, plus `Server-Timing: app;dur=<ms>`.

**Pagination.** List endpoints take `page` (1-based) and `page_size` and return `items`, `total`, `page`,
`page_size` (v2/v3 also `has_more`). Maximum `page_size` is 100–200 depending on the endpoint (see OpenAPI).

**Time and money.** Timestamps are ISO 8601 with a UTC offset. Prices are decimal strings (`"499.00"`) with an
ISO 4217 `currency`; prices are never converted between currencies, and price filters across currencies are
refused (`currency` is required when the catalog has more than one).

**Versioning.** The path carries the major version (`/api/v1`, `/api/v2`, `/api/v3`). Response documents carry a
schema id (`agentready-catalog/2.0`, `agentready-product/1.0`, `agentready-feed/1.0`). Fields are only added within a
version; removals or meaning changes get a new version. `/api/v1/catalog` keeps its Phase 1 shape (private
attribute rows are now filtered out of it, as in every external API).

**Rate limits.** Auth endpoints: 10/min per IP (+ 10 failed logins per 15 min per account). API keys: per key,
default 120/min (configurable 1–10,000). Anonymous agent access: 60/min per IP. Feed tokens: per token. Exceeding
a limit returns 429 with `Retry-After`.

## Merchant API (`/api/v1`) — main groups

| Group | Endpoints |
|---|---|
| Auth | `POST auth/register`, `POST auth/login`, `POST auth/logout`, `GET/PATCH auth/me`, `POST auth/forgot-password`, `POST auth/reset-password`, `POST auth/change-password` |
| Websites & scans | `websites` (+ `validate-url`), `scans`, `scans/{id}` (`/progress`, `/cancel`, `/changes`, `/apply`, `/pages`) |
| Products | `products` (list/filter/sort, create), `products/{id}` (GET/PATCH/DELETE, `/history`, `/archive`, `/restore`, `/variants`, `/pricing`, `/inventory`, `/price-history`), `products/facets`, `products/bulk-approval` |
| Commerce | `categories`, `attribute-definitions`, `variants`, `pricing` (scheduled changes), `inventory` (`adjust`, transactions, low stock), `bulk-operations` (preview, run, status) |
| Validation | `validation/issues`, `validation/summary`, `validation/issues/{id}/resolve` |
| Sync & integrations | `sync` (schedules, jobs, conflicts + resolution), `integrations` (WooCommerce, Shopify OAuth/token, import, push, webhooks) |
| Distribution | `distribution/settings`, `distribution/products` (+ preview), `distribution/feeds` (+ `generate`, `snapshots`, `tokens`), `distribution/webhooks` (+ deliveries, redeliver, rotate), `distribution/platforms`, `distribution/analytics`, `api-keys` |
| Intelligence | `intelligence/overview`, `catalog-quality`, `products/{id}/quality`, `search-insights`, `zero-result-queries`, `recommendations` (+ dismiss/reopen/apply), `anomalies`, `pricing-insights`, `inventory-insights`, `reports` (+ download), `notifications`, `assistant/query`, `assistant/suggestions` (+ approve/reject), `settings`, `refresh` |
| Exports & settings | `exports` (+ download), `business`, `business/catalog-key`, `settings/ai` (+ verify), `settings/catalog-rules`, `dashboard/overview`, `dashboard/charts`, `audit-logs` |
| System | `GET system/status` — dependency health + this merchant's jobs and recent failures |

Example — sign in and list products:

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","password":"DemoPass2026!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s "http://localhost:8080/api/v1/products?page_size=5" -H "Authorization: Bearer $TOKEN"
```

## Catalog API (`/api/v2/catalog`)

Create a key in **Catalog API v2** (scopes `catalog:read`, `pricing:read`, `inventory:read`, `evidence:read`).
Only approved + published + active/discontinued products are visible.

| Endpoint | Scope | Notes |
|---|---|---|
| `GET products`, `GET products/{id}`, `GET products/{id}/variants`, `GET search`, `GET categories` | `catalog:read` | price only with `pricing:read`, `available_quantity` only with `inventory:read` |
| `GET pricing` | `pricing:read` | regular/sale/effective price and status |
| `GET inventory`, `GET availability` | `inventory:read` / `catalog:read` | stock status; quantities only with `inventory:read` |
| `GET feed?type=full\|incremental\|category\|availability` | `catalog:read` | incremental: `updated_since=` → changed items + `removed` tombstones + `next_updated_since` |

## Agent API (`/api/v3/agents`)

Requires public distribution to be switched on by the merchant (otherwise 403 `distribution_disabled`).

| Endpoint | Purpose |
|---|---|
| `GET capabilities` (also `/.well-known/agentready.json`) | discovery document: endpoints, auth, limits, data policy |
| `GET search`, `GET products` | `q`, `brand`, `sku`, `gtin`, `category`, `min_price`/`max_price` + `currency`, `availability`, `merchant`, repeatable `attr=voltage:230 V` / `attr=storage>=512 GB`, `variant=size:M`, `sort=relevance\|price_asc\|price_desc\|name\|updated`, `page`, `page_size` (≤100). Ranking reasons are returned per item. |
| `GET products/{id}` (+ `/variants`, `/availability`, `/pricing`) | public representation `agentready-product/1.0`; availability is a status, never a quantity |
| `GET/POST compare` | side-by-side comparison with unit-aware attribute alignment |
| `POST recommend` | constraint-based suggestions from the catalog (no paid placement) |
| `GET categories`, `GET merchants`, `GET merchants/{id}/products` | browsing |
| `GET changes?since=` | incremental changes with tombstones |
| `GET tools`, `POST tools/{name}` | the MCP tools over plain REST |
| `POST carts`, `GET carts/{id}`, `POST carts/{id}/checkout`, `GET checkout-sessions/{id}` | validated hand-off to the merchant's own checkout (`checkout:write`); **no payment is processed** |

```bash
curl -s -H "X-Catalog-Key: ar_cat_..." "http://localhost:8080/api/v3/agents/search?q=drill&attr=voltage:18%20V"
```

Every response includes `unknown_fields` (data AgentReady does not have) and `withheld_fields` (data the merchant
chose not to share) so agents can tell "missing" from "private".

## Feeds

Configure feeds in **Distribution → Product feeds** (formats `json`, `jsonl`, `csv`, `jsonld`, `xml`; profiles
`agentready`, `openai`, `google`; types full, incremental, availability, pricing, category, variant). Each
generation is a versioned snapshot with a validation report. A feed URL (`/api/v3/feeds/arf_…`) serves the latest
completed snapshot; tokens are revocable, can expire and are rate-limited. Format details:
[docs/feed-format.md](docs/feed-format.md).

## Webhooks

Subscriptions receive `product.created`, `product.updated`, `product.published`, `product.unpublished`,
`product.price_changed`, `product.availability_changed`, `product.variant_updated` and `product.archived` events
(payload: the public representation, or a tombstone when a product leaves distribution), signed per Standard Webhooks (`webhook-id`, `webhook-timestamp`,
`webhook-signature: v1,<base64 HMAC-SHA256>`). Delivery: HTTPS public endpoints only, no redirects, 8 attempts with
exponential backoff and jitter, replay keeps the same `webhook-id`, both secrets valid for 24 h after rotation.

## Health

* `GET /health/live` → `{"status":"ok"}` while the process serves HTTP.
* `GET /health` → `{"status":"ok|degraded|unavailable","checks":{"database":…,"redis":…,"worker":…,"scheduler":…}}`;
  HTTP 503 when the database or Redis is down; `degraded` (200) when no worker answers or the scheduler heartbeat
  is older than 3 minutes. See [DEPLOYMENT.md](DEPLOYMENT.md#health-checks).
