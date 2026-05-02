#!/usr/bin/env bash
set -euo pipefail

# Production backup helper. It writes a custom-format pg_dump that can be
# smoke-restored by scripts/restore_postgres_smoke.sh before release.

OUT_DIR="${1:-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"

if [[ -z "${KUN_PG_ADMIN_DSN:-}" ]]; then
  echo "KUN_PG_ADMIN_DSN is required for backup" >&2
  exit 2
fi

OUT_FILE="$OUT_DIR/kun-postgres-$STAMP.dump"
pg_dump "$KUN_PG_ADMIN_DSN" --format=custom --no-owner --no-privileges --file="$OUT_FILE"
sha256sum "$OUT_FILE" > "$OUT_FILE.sha256"
echo "$OUT_FILE"
