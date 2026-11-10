#!/bin/sh
# Celery worker entrypoint: wait for PostgreSQL, then consume jobs. Warm shutdown on SIGTERM lets the
# running task finish (acks_late + reject_on_worker_lost requeue it if the container is killed instead).
set -e
python -m app.db.wait --timeout "${DB_WAIT_TIMEOUT:-90}"
exec celery -A app.workers.celery_app worker --loglevel="${LOG_LEVEL:-INFO}" \
  --concurrency="${WORKER_CONCURRENCY:-2}" --hostname="worker@%h"
