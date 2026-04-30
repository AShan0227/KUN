#!/usr/bin/env bash
set -euo pipefail

# Smoke-restore a backup into a disposable database URL.
# Usage:
#   KUN_RESTORE_TEST_DSN=postgresql://user:pass@host:port/db \
#     scripts/restore_postgres_smoke.sh backups/kun-postgres-*.dump

BACKUP_FILE="${1:-}"
if [[ -z "$BACKUP_FILE" || ! -f "$BACKUP_FILE" ]]; then
  echo "backup dump file is required" >&2
  exit 2
fi
if [[ -z "${KUN_RESTORE_TEST_DSN:-}" ]]; then
  echo "KUN_RESTORE_TEST_DSN is required for smoke restore" >&2
  exit 2
fi

pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$KUN_RESTORE_TEST_DSN" "$BACKUP_FILE"
psql "$KUN_RESTORE_TEST_DSN" -v ON_ERROR_STOP=1 -c "SELECT COUNT(*) FROM information_schema.tables;" >/dev/null
echo "restore smoke passed: $BACKUP_FILE"
