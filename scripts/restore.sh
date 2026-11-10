#!/bin/sh
# Restore a backup made by backup.sh.
#   ./scripts/restore.sh backups/agentready-....dump               -> into a NEW database agentready_restore
#   ./scripts/restore.sh backups/agentready-....dump mydb          -> into a NEW database "mydb"
#   ./scripts/restore.sh backups/agentready-....dump agentready --replace-live
#        replaces the live database (stop api/worker/beat first; see DEPLOYMENT.md "Restore").
# The default never touches the live database: restore into a copy, verify it, then switch if needed.
set -eu
# Git Bash on Windows would rewrite /tmp/... container paths into Windows paths
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
COMPOSE="docker compose ${COMPOSE_FILES:-}"
FILE=${1:?usage: restore.sh <backup.dump> [target_db] [--replace-live]}
TARGET=${2:-agentready_restore}
MODE=${3:-}
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }
if [ "$TARGET" = "agentready" ] && [ "$MODE" != "--replace-live" ]; then
  echo "refusing to restore over the live database without --replace-live" >&2; exit 1
fi
BASENAME=$(basename "$FILE")
$COMPOSE cp "$FILE" "postgres:/tmp/$BASENAME"
if [ "$TARGET" = "agentready" ]; then
  $COMPOSE exec -T postgres psql -U agentready -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='agentready' AND pid<>pg_backend_pid();" \
    -c "DROP DATABASE agentready;" -c "CREATE DATABASE agentready OWNER agentready;"
else
  EXISTS=$($COMPOSE exec -T postgres psql -U agentready -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$TARGET'")
  [ -z "$EXISTS" ] || { echo "database $TARGET already exists; choose another name or drop it first" >&2; exit 1; }
  $COMPOSE exec -T postgres createdb -U agentready -O agentready "$TARGET"
fi
$COMPOSE exec -T postgres pg_restore -U agentready -d "$TARGET" --no-owner --exit-on-error "/tmp/$BASENAME"
$COMPOSE exec -T postgres rm -f "/tmp/$BASENAME"
echo "restored into database: $TARGET"
$COMPOSE exec -T postgres psql -U agentready -d "$TARGET" -tAc \
  "SELECT 'alembic_version='||version_num FROM alembic_version UNION ALL
   SELECT 'businesses='||count(*) FROM businesses UNION ALL
   SELECT 'products='||count(*) FROM products UNION ALL
   SELECT 'audit_logs='||count(*) FROM audit_logs"
