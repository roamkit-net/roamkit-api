# roamkit-api — Django + DRF + Celery

Application API for RoamKit. See [roamkit-docs](https://github.com/roamkit-net/roamkit-docs) for architecture and standards.

## Layout

```
src/
├── config/          # settings (base, dev, staging, production), urls, celery
├── core/            # health endpoints, shared primitives
├── shared/          # events, utils
└── apps/            # domain apps (accounts, catalog, …) — added in later phases
```

## Local development

Requires PostGIS and Redis from `roamkit-infra`:

```bash
cd ../roamkit-infra/docker
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d
```

Then in this repo:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
export DJANGO_SETTINGS_MODULE=config.settings.dev
export POSTGRES_HOST=localhost POSTGRES_PORT=5432
export POSTGRES_DB=roamkit POSTGRES_USER=roamkit POSTGRES_PASSWORD=change-me-local
export REDIS_URL=redis://localhost:6379/0
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Health checks:

- `GET /health/live` — process liveness
- `GET /health/ready` — database + Redis readiness

## Tests

```bash
docker compose -f docker/docker-compose.test.yml up -d --wait
export POSTGRES_HOST=localhost POSTGRES_PORT=5433
export POSTGRES_DB=roamkit_test POSTGRES_USER=roamkit POSTGRES_PASSWORD=roamkit_test
export REDIS_URL=redis://localhost:6380/0
pytest
docker compose -f docker/docker-compose.test.yml down -v
```

### Migration smoke (Order.user → Order.account)

CI runs a dump → migrate → verify path before the main pytest job. Locally:

```bash
docker compose -f docker/docker-compose.test.yml up -d --wait
export POSTGRES_HOST=localhost POSTGRES_PORT=5433
export POSTGRES_USER=roamkit POSTGRES_PASSWORD=roamkit_test
export REDIS_URL=redis://localhost:6380/0
./scripts/run_migration_smoke.sh
```

The fixture dump (`tests/fixtures/migration_smoke/pre_billing.sql`) is a small pre-billing snapshot (users + orders across statuses). Regenerate with `./scripts/build_migration_smoke_dump.sh` after intentional schema changes below that baseline.

Phase 2 DoD coverage lives in `tests/test_phase2_dod.py` (register → `create_sandbox_esim` → `me/esims` + usage + isolation). On staging after deploy, run `roamkit-infra/scripts/staging-dod-faza2.sh` (use `CREATE_SANDBOX=1` on the host to fulfill a sandbox eSIM).

## CI / deploy

Workflows in `.github/workflows/` build and push `ghcr.io/roamkit-net/roamkit-api` on merge to `develop`, then deploy to staging via SSH.

Staging settings (`config.settings.staging`) trust Traefik proxy headers per [ADR 009](https://github.com/roamkit-net/roamkit-docs/blob/develop/docs/adr/009-shared-traefik-edge.md).
