#!/bin/sh
# API container entrypoint: wait for PostgreSQL, apply migrations (unless disabled), start uvicorn.
#   RUN_MIGRATIONS=false   when a separate one-shot migration step runs them (production compose)
#   FORWARDED_ALLOW_IPS    proxies whose X-Forwarded-For is trusted (the nginx container network)
#   WEB_CONCURRENCY        uvicorn worker processes (default 2)
#   PORT                   listen port (default 8000; Render and similar hosts set it)
set -e
# Public URL defaults (Render sets RENDER_EXTERNAL_URL); explicit FRONTEND_URL/CORS_ORIGINS/API_BASE_URL win.
PUBLIC_URL="${PUBLIC_URL:-${RENDER_EXTERNAL_URL:-}}"
if [ -n "$PUBLIC_URL" ]; then
  export FRONTEND_URL="${FRONTEND_URL:-$PUBLIC_URL}" CORS_ORIGINS="${CORS_ORIGINS:-$PUBLIC_URL}"          API_BASE_URL="${API_BASE_URL:-$PUBLIC_URL}"
fi
python -m app.db.wait --timeout "${DB_WAIT_TIMEOUT:-90}"
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --workers "${WEB_CONCURRENCY:-2}" --timeout-graceful-shutdown 20
