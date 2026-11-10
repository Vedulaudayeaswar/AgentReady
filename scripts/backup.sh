#!/bin/sh
# PostgreSQL backup (custom format, compressed) of the running compose stack.
#   ./scripts/backup.sh                 -> backups/agentready-YYYYMMDD-HHMMSS.dump
# Works while the app is running (pg_dump takes a consistent snapshot). Add -f docker-compose.prod.yml
# via COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml" for production.
set -eu
# Git Bash on Windows would rewrite /tmp/... container paths into Windows paths
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
COMPOSE="docker compose ${COMPOSE_FILES:-}"
STAMP=$(date -u +%Y%m%d-%H%M%S)
NAME="agentready-$STAMP.dump"
mkdir -p backups
$COMPOSE exec -T postgres pg_dump -U agentready -d agentready -Fc -Z 6 -f "/tmp/$NAME"
# verify the archive is complete and readable before keeping it
TABLES=$($COMPOSE exec -T postgres sh -c "pg_restore --list /tmp/$NAME | grep -c 'TABLE DATA'")
$COMPOSE cp "postgres:/tmp/$NAME" "backups/$NAME"
$COMPOSE exec -T postgres rm -f "/tmp/$NAME"
echo "archive verified: $TABLES tables with data"
SIZE=$(wc -c < "backups/$NAME" | tr -d ' ')
echo "backup written: backups/$NAME ($SIZE bytes)"
