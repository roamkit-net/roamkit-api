# OpenAPI schema

Committed artifact: [`openapi.yaml`](./openapi.yaml).

## Generate (only supported path)

From the `roamkit-api` repo root, with Python deps installed from `requirements/base.txt`
(or `requirements/dev.txt`):

```bash
./scripts/generate_openapi.sh
```

Validate without changing workflow:

```bash
./scripts/generate_openapi.sh --validate-only
# or
python manage.py spectacular --file openapi/openapi.yaml --validate
```

Local and CI must use the same pinned `drf-spectacular` version from `requirements/base.txt`
so the YAML stays identical.

## Served URLs

| Path | Purpose |
|------|---------|
| `/api/schema/` | OpenAPI 3 schema (YAML/JSON) |
| `/api/docs/` | Swagger UI |
| `/api/redoc/` | ReDoc |

Staging: `https://api.staging.roamkit.net/api/schema/`

## Wave 2 backlog (not C10)

Frontend type generation is intentionally deferred:

- Generate types with `openapi-typescript` from this YAML.
- Emit into `roamkit-web/src/api/generated/` as **read-only** output (no hand edits).
- Add web CI so generated clients stay in sync with this artifact.
