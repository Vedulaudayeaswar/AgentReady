# Deployment

How to run AgentReady on a single Linux server with Docker Compose, HTTPS, backups and a rollback path.
Local setup is in [INSTALLATION.md](INSTALLATION.md).

## 1. Production topology

`docker compose -f docker-compose.yml -f docker-compose.prod.yml` runs:

| Service | Exposed | Notes |
|---|---|---|
| `web` (nginx) | **80, 443** (the only published ports) | TLS termination, HSTS, CSP, SPA, `/api` proxy |
| `api` | internal only | `ENVIRONMENT=production`, JSON logs, 2 uvicorn workers (`WEB_CONCURRENCY`), 1 GB memory limit |
| `migrate` | — | one-shot `alembic upgrade head` before `api`/`worker`/`beat` start |
| `worker` | internal | Celery, `WORKER_CONCURRENCY` (2), 2 GB limit, processes recycled every 200 tasks |
| `beat` | internal | exactly **one** instance |
| `postgres` | internal only | volume `pgdata` |
| `redis` | internal only | append-only persistence, `noeviction`, volume `redisdata` |

All services have `restart: unless-stopped`; `api`, `worker` and `web` have health checks.

## 2. Production configuration

Create `.env` on the server from `.env.example`. Required (the API **refuses to start** in production when any
check fails, and prints which one):

| Variable | Value |
|---|---|
| `ENVIRONMENT` | set by the prod overlay to `production` |
| `SECRET_KEY` | ≥ 32 random characters: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `CREDENTIALS_ENCRYPTION_KEY` | Fernet key: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` — **back it up separately**; without it, stored integration credentials and webhook secrets cannot be decrypted |
| `POSTGRES_PASSWORD` | strong random password |
| `PUBLIC_URL` | `https://catalog.example.com` (sets `FRONTEND_URL`, `CORS_ORIGINS`, `API_BASE_URL`) |
| `SERVER_NAME` | `catalog.example.com` |
| `GROQ_API_KEY` | optional |
| `SHOPIFY_API_KEY` / `SHOPIFY_API_SECRET` | optional, for Shopify OAuth |

Enforced automatically: `ALLOW_PRIVATE_NETWORKS=false`, `JOB_BACKEND=celery`, PostgreSQL, no `AI_PROVIDER=mock`,
no `CORS_ORIGINS=*`, `/docs` disabled (set `API_DOCS_ENABLED=true` to expose it), `LOG_FORMAT=json`.

## 3. TLS certificates

nginx expects `deploy/certs/fullchain.pem` and `deploy/certs/privkey.pem` (git-ignored). With Let's Encrypt:

```bash
mkdir -p deploy/certs deploy/certbot
# first issuance (standalone, before nginx is running):
docker run --rm -p 80:80 -v "$PWD/deploy/letsencrypt:/etc/letsencrypt" certbot/certbot certonly --standalone \
  -d catalog.example.com --agree-tos -m ops@example.com --non-interactive
cp deploy/letsencrypt/live/catalog.example.com/fullchain.pem deploy/letsencrypt/live/catalog.example.com/privkey.pem deploy/certs/
# renewal (webroot; nginx serves /.well-known/acme-challenge/ from deploy/certbot), e.g. weekly cron:
docker run --rm -v "$PWD/deploy/letsencrypt:/etc/letsencrypt" -v "$PWD/deploy/certbot:/var/www/certbot" certbot/certbot \
  renew --webroot -w /var/www/certbot && cp deploy/letsencrypt/live/catalog.example.com/*.pem deploy/certs/ \
  && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec web nginx -s reload
```

Behind a managed load balancer that terminates TLS instead, keep the HTTP server config and make sure the load
balancer is the only client of port 80.

## 4. First deployment

```bash
git clone <repository> /opt/agentready && cd /opt/agentready
cp .env.example .env && chmod 600 .env        # fill in section 2
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE config -q                             # validates the files and required variables
$COMPOSE up -d --build
$COMPOSE ps                                    # migrate: Exited (0); others Up / healthy
curl -s https://catalog.example.com/health     # {"status":"ok", ...}
```

Then open `https://catalog.example.com/signup`, create the first merchant, and check **System status** in the app.

## 5. Database migrations

Migrations are in `backend/alembic/versions` (current head: **0006**). The prod overlay runs them once per
deployment in the `migrate` service before the API starts. Every migration so far is additive (new tables,
nullable columns, indexes) and has a downgrade.

Procedure for a release that contains migrations:

1. **Back up first** (section 7).
2. Read the new migration files; anything that rewrites large tables should be scheduled for a quiet period.
3. `$COMPOSE up -d --build` — `migrate` applies them; check `$COMPOSE logs migrate`.
4. Verify: `$COMPOSE exec postgres psql -U agentready -tAc "select version_num from alembic_version"`.

Manual commands: `$COMPOSE run --rm migrate alembic current`, `... alembic history`, `... alembic upgrade head`.

## 6. Updating (zero-surprise procedure)

```bash
cd /opt/agentready
./scripts/backup.sh                          # COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
git fetch && git checkout <release-tag>
$COMPOSE build
$COMPOSE up -d                               # migrate -> api/worker/beat restart; web restarts last
curl -s https://catalog.example.com/health
```

Workers finish their current task on SIGTERM (60 s grace); interrupted tasks are redelivered
(`acks_late` + `reject_on_worker_lost`), and jobs left running by a crash are recovered by the scheduler
(resumed if idempotent, otherwise marked failed with a reason and shown on **System status**).

## 7. Backups

```bash
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml" ./scripts/backup.sh
# -> backups/agentready-YYYYMMDD-HHMMSS.dump (pg_dump custom format, compressed, verified with pg_restore --list)
```

Windows: `.\scripts\backup.ps1`. Schedule it (cron/systemd timer) at least daily, copy `backups/` off the server
(object storage), and keep the `.env` (especially `CREDENTIALS_ENCRYPTION_KEY`) in a separate secret store. The dump
contains merchant data — store it encrypted. Redis holds only queued jobs and caches; it does not need backups.

## 8. Restore

Restore into a **new** database first and verify it — this never touches the live data:

```bash
./scripts/restore.sh backups/agentready-20261108-180429.dump            # -> database agentready_restore
# prints alembic_version and row counts for businesses / products / audit_logs
```

To replace the live database (outage):

```bash
$COMPOSE stop api worker beat web
./scripts/restore.sh backups/agentready-....dump agentready --replace-live
$COMPOSE up -d
```

The restore procedure was tested in Phase 5 on a copy of the Docker data: counts in the restored database matched
the live database (15 merchants, 392 products, 220 audit entries, migration 0005), and the application's ORM and
Alembic read the restored copy (`alembic current` → head).

## 9. Rollback

| Situation | Action |
|---|---|
| New release misbehaves, **no** migration in it | `git checkout <previous-tag> && $COMPOSE up -d --build` |
| New release with an additive migration (all migrations so far) | roll the code back as above; the extra columns/tables are ignored by the old code. Downgrade only if required: `$COMPOSE run --rm migrate alembic downgrade <previous revision>` |
| Data damaged | stop the app and restore the last good backup (section 8) |

Always take a backup before a downgrade: downgrades drop the columns/tables that the migration added.

## 10. Health checks

| Check | Use | Healthy |
|---|---|---|
| `GET /health/live` | container liveness (Docker `HEALTHCHECK`) | `200 {"status":"ok"}` |
| `GET /health` | readiness / uptime monitor | `200`, `status: ok` |
| `celery inspect ping` | worker container health check | a reply from `worker@<host>` |
| **System status** page | merchant-visible: dependencies + failed jobs with retry hints | — |

`/health` meanings:

* `ok` — database answered `SELECT 1`, Redis answered `PING`, at least one worker answered a ping, and the
  scheduler wrote a heartbeat in the last 3 minutes.
* `degraded` (200) — the app serves requests but background work is stopped (no worker / stale scheduler).
  Check `$COMPOSE ps` and `$COMPOSE logs worker beat`.
* `unavailable` (503) — database or Redis unreachable. API requests that need the database return 503 with
  `Retry-After` instead of 500 errors.

Point an external uptime monitor at `https://<domain>/health` and alert on non-200 or `status != ok`.

## 11. Logs and monitoring

* `$COMPOSE logs -f api worker beat web`. API logs are JSON lines in production (`ts`, `level`, `logger`,
  `message`, `request_id`, and `http_method`/`http_path`/`http_status`/`duration_ms` for requests). Requests slower
  than 2 s or with 5xx status are logged at WARNING.
* The same `request_id` appears in the nginx access log (`rid=`), the API log, error responses and audit entries.
* Secrets are redacted from all logs. Configure Docker log rotation (e.g. `/etc/docker/daemon.json`:
  `{"log-driver":"json-file","log-opts":{"max-size":"50m","max-file":"5"}}`).

## 12. Capacity notes

Measured on a laptop (Docker Desktop, in-process requests) with 5,000 products — see
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md#performance-and-scaling) for the full table. Agent search, MCP and product
detail answer in ~40–90 ms and catalog listing in ~100–160 ms; intelligence pages are O(catalog size) (~2 s at 5,000
products).
Scale by adding worker replicas (`$COMPOSE up -d --scale worker=3`) — never scale `beat`. The API can run more
uvicorn workers (`WEB_CONCURRENCY`); rate limits and caches are shared through Redis.
