# Troubleshooting

Start with two things: **`GET /health`** (which dependency is down) and the in-app **System status** page (which
background jobs failed and why). Every error response contains a `request_id`; search the logs for it:
`docker compose logs api worker | grep <request_id>`.

## Startup

| Symptom | Cause / fix |
|---|---|
| `api` exits with `Refusing to start in production: ...` | The message lists each unsafe setting (short `SECRET_KEY`, missing/invalid `CREDENTIALS_ENCRYPTION_KEY`, `ALLOW_PRIVATE_NETWORKS=true`, `AI_PROVIDER=mock`, `CORS_ORIGINS=*`, inline jobs, SQLite). Fix `.env` and `docker compose up -d`. |
| `api` logs `waiting for database (...)` repeatedly, then exits | PostgreSQL not reachable within 90 s (`DB_WAIT_TIMEOUT`). Check `docker compose ps postgres` and `docker compose logs postgres`; the container restarts automatically. |
| `Bind for 127.0.0.1:5432 failed: port is already allocated` | Another PostgreSQL uses the port: set `POSTGRES_HOST_PORT=55432` (only affects host access). |
| `POSTGRES_PASSWORD must be set` / `PUBLIC_URL must be set` | Production overlay requires them in `.env`. |
| `migrate` service `Exited (1)` | A migration failed: `docker compose logs migrate`. Nothing after it starts. Restore the backup taken before the update if the database is in an unexpected state. |
| web shows "Cannot reach the AgentReady API" | `api` not running or unhealthy: `docker compose ps api`, `docker compose logs api --tail 50`. |
| `exec docker/start-api.sh: no such file` / `\r: not found` | Scripts checked out with CRLF line endings outside Docker; the image strips them, so rebuild: `docker compose build api worker`. |

## Health is `degraded` or `unavailable`

| `checks.<name>.status = error` | Meaning | Fix |
|---|---|---|
| `database` | PostgreSQL down or credentials wrong (requests return 503) | `docker compose logs postgres`; check `POSTGRES_PASSWORD` matches the volume's original password |
| `redis` | broker down (no jobs, rate limits fall back to memory) | `docker compose restart redis` |
| `worker` — "no worker answered" | Celery worker stopped or busy past the 1.5 s ping | `docker compose ps worker`, `docker compose logs worker --tail 100`; long scans do not block pings (each task runs in its own process) |
| `scheduler` — "last tick Ns ago" | Celery Beat not running: no scheduled rescans, price changes, feed regeneration, intelligence passes or job recovery | `docker compose restart beat` — run exactly one beat |

## Scans

| Symptom | Cause / fix |
|---|---|
| "This address is not allowed" when connecting a site | The URL resolves to a private/loopback/metadata address or uses a blocked port (SSRF protection). Use the public website address. |
| Scan finishes with few or no products | robots.txt disallows pages, products need JavaScript (enable `PLAYWRIGHT_ENABLED` with the Playwright image), or the site blocks bots. The scan page lists skipped/failed pages with reasons. |
| Scan stuck "running" | If the worker died, the scan is marked failed after `SCAN_STALE_AFTER_MINUTES` (15) by the scheduler with "stopped reporting progress"; start a new scan. |
| "AI usage: not configured" | `GROQ_API_KEY` missing; extraction stays deterministic. |
| Groq errors (401/404 model) | **Settings → AI usage → Verify model**; check the key and `GROQ_MODEL`. 429s are retried automatically. |

## Distribution, feeds, webhooks

| Symptom | Cause / fix |
|---|---|
| Agent API returns `403 distribution_disabled` | Switch on **Distribution → Overview → Public distribution** and the channel (agent API / MCP / feeds). |
| A product is missing from the agent API or feeds | It must be approved, published, active/discontinued and selected for distribution — **Public products → Preview** shows why a product is excluded. |
| Feed snapshot `failed` with "Interrupted" | The worker restarted during generation; select **Generate now**. |
| Feed URL returns 401/404 | The token was revoked, expired or rotated — create a new feed URL. |
| Webhook deliveries `failed` | The receiver must be HTTPS on a public address and answer 2xx within 10 s without redirects. **Webhooks → Redeliver** retries with the same `webhook-id`. |
| MCP client cannot connect | See [MCP_INTEGRATION.md](MCP_INTEGRATION.md#troubleshooting). |

## Sign-in

| Symptom | Cause / fix |
|---|---|
| "Too many failed sign-in attempts for this account" (429) | 10 failed attempts in 15 minutes for that email. Wait, or reset the password (which clears the counter). |
| "Too many requests" (429) on login/registration | Per-IP limit (10/min). Behind a proxy other than the bundled nginx, make sure it overwrites `X-Forwarded-For` and set `FORWARDED_ALLOW_IPS` to its address — otherwise all users share one limit. |
| Signed out after ~30 minutes | Sessions last `ACCESS_TOKEN_EXPIRE_MINUTES` (30) and are per browser tab. |
| Password-reset link never arrives | No e-mail provider is configured. In development the link is shown on screen; in production an operator must reset it (see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)). |

## Performance

* Slow requests (> 2 s) are logged at WARNING by `agentready.http` with `duration_ms`; the `Server-Timing` response
  header shows server time per request in the browser's network tab.
* Intelligence pages load all quality results of the merchant per request (~1–2 s at 5,000 products).
* `eval/perf_benchmark.py` reproduces the measurements against a disposable database.

## Data

| Task | Command |
|---|---|
| Back up | `./scripts/backup.sh` (Windows: `.\scripts\backup.ps1`) |
| Test a restore (safe) | `./scripts/restore.sh backups/<file>.dump` → database `agentready_restore` |
| Check migration state | `docker compose exec postgres psql -U agentready -tAc "select version_num from alembic_version"` |
| Open a SQL shell | `docker compose exec postgres psql -U agentready agentready` |
