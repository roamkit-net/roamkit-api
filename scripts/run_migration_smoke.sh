#!/usr/bin/env bash
# Migration smoke: restore pre-billing dump → migrate to HEAD → verify → pytest.
#
# Intended for CI. Requires PostGIS (docker/docker-compose.test.yml) and Python deps.
#
# Usage (from roamkit-api root):
#   ./scripts/run_migration_smoke.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.dev}"
export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5433}"
export POSTGRES_USER="${POSTGRES_USER:-roamkit}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-roamkit_test}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6380/0}"

SMOKE_DB="${MIGRATION_SMOKE_DB:-roamkit_mig_smoke}"
DUMP="$ROOT/tests/fixtures/migration_smoke/pre_billing.sql"
CONTAINER="${POSTGIS_CONTAINER:-roamkit-postgis-test}"

run_py() {
  if [[ -n "${DJANGO_RUNNER:-}" ]]; then
    # shellcheck disable=SC2086
    eval "$DJANGO_RUNNER" "$@"
  else
    python "$@"
  fi
}

if [[ ! -f "$DUMP" ]]; then
  echo "Missing dump: $DUMP" >&2
  echo "Generate with: ./scripts/build_migration_smoke_dump.sh" >&2
  exit 1
fi

echo "==> Recreating smoke database ${SMOKE_DB}"
docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
  psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '${SMOKE_DB}' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ${SMOKE_DB};
CREATE DATABASE ${SMOKE_DB} OWNER ${POSTGRES_USER};
SQL

echo "==> Restoring pre-billing dump"
docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
  psql -U "$POSTGRES_USER" -d "$SMOKE_DB" -v ON_ERROR_STOP=1 < "$DUMP"

export POSTGRES_DB="$SMOKE_DB"
export PYTHONPATH="${PYTHONPATH:-}:$ROOT/src"
export MIGRATION_SMOKE=1

echo "==> Running migrations to HEAD"
run_py manage.py migrate --noinput

echo "==> Verifying backfill integrity"
run_py scripts/verify_migration_smoke.py

echo "==> Running pytest against migrated smoke DB"
if [[ -n "${DJANGO_RUNNER:-}" ]]; then
  # shellcheck disable=SC2086
  eval "$DJANGO_RUNNER" -m pytest tests/migration_smoke/ -q --tb=short
else
  pytest tests/migration_smoke/ -q --tb=short
fi

echo "==> Migration smoke OK"
