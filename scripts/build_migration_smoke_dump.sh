#!/usr/bin/env bash
# Build a pre-billing PostgreSQL dump for migration smoke tests.
#
# Snapshot = schema after Faza 2 (Order.user still present; no billing app).
# Seeded with a few users and orders across statuses.
#
# Usage (from roamkit-api root, with test PostGIS up on :5433):
#   ./scripts/build_migration_smoke_dump.sh
#
# Requires: docker (postgis container), Python 3.12+ with requirements/dev.txt
# (or set DJANGO_RUNNER to a wrapper, e.g. docker run … python).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.dev}"
export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5433}"
export POSTGRES_USER="${POSTGRES_USER:-roamkit}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-roamkit_test}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6380/0}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

BUILD_DB="roamkit_mig_smoke_build"
OUT_DIR="$ROOT/tests/fixtures/migration_smoke"
OUT_FILE="$OUT_DIR/pre_billing.sql"
CONTAINER="${POSTGIS_CONTAINER:-roamkit-postgis-test}"

run_django() {
  if [[ -n "${DJANGO_RUNNER:-}" ]]; then
    # shellcheck disable=SC2086
    eval "$DJANGO_RUNNER" "$@"
  else
    python "$@"
  fi
}

echo "==> Recreating build database ${BUILD_DB}"
docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
  psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '${BUILD_DB}' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ${BUILD_DB};
CREATE DATABASE ${BUILD_DB} OWNER ${POSTGRES_USER};
SQL

export POSTGRES_DB="$BUILD_DB"

echo "==> Migrating to pre-billing schema"
run_django manage.py migrate contenttypes --noinput
run_django manage.py migrate auth --noinput
run_django manage.py migrate accounts 0001_initial --noinput
run_django manage.py migrate admin --noinput
run_django manage.py migrate sessions --noinput
run_django manage.py migrate catalog 0004_location_coverages --noinput
run_django manage.py migrate orders 0001_initial --noinput
run_django manage.py migrate esims 0001_initial --noinput

echo "==> Seeding snapshot data"
run_django scripts/seed_migration_smoke.py

mkdir -p "$OUT_DIR"
echo "==> Dumping to ${OUT_FILE}"
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
  pg_dump -U "$POSTGRES_USER" -d "$BUILD_DB" \
  --no-owner --no-privileges --clean --if-exists \
  > "$OUT_FILE"

echo "==> Done ($(wc -l < "$OUT_FILE") lines)"
