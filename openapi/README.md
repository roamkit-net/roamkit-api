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
