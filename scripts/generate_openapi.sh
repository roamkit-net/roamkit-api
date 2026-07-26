#!/usr/bin/env bash
# Generate the committed OpenAPI artifact.
#
# Deterministic contract: always run this script (local and CI) with deps from
# requirements/base.txt (and requirements/dev.txt when developing). Do not invoke
# `manage.py spectacular` with ad-hoc flags that change output shape.
#
# Usage (from repo root, with PYTHONPATH/Django env already workable):
#   ./scripts/generate_openapi.sh
#   ./scripts/generate_openapi.sh --validate-only
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.dev}"
export PYTHONPATH="${PYTHONPATH:-}:${ROOT}/src"

OUT="${ROOT}/openapi/openapi.yaml"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

VALIDATE_ONLY=0
if [[ "${1:-}" == "--validate-only" ]]; then
  VALIDATE_ONLY=1
fi

python manage.py spectacular --file "$TMP" --validate

# Stable key ordering for reviewable diffs (paths + components).
python - "$TMP" "$OUT" <<'PY'
import sys
from pathlib import Path

import yaml

src, dest = Path(sys.argv[1]), Path(sys.argv[2])


def sort_obj(obj):
    if isinstance(obj, dict):
        return {k: sort_obj(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [sort_obj(x) for x in obj]
    return obj


data = yaml.safe_load(src.read_text(encoding="utf-8"))
# Drop volatile / non-contract metadata if present.
data.pop("x-tagGroups", None)
if isinstance(data.get("info"), dict):
    data["info"].pop("x-logo", None)


def dedupe_security(sec):
    if not isinstance(sec, list):
        return sec
    seen = []
    for item in sec:
        if item not in seen:
            seen.append(item)
    return seen


if isinstance(data.get("security"), list):
    data["security"] = dedupe_security(data["security"])
for _path, methods in (data.get("paths") or {}).items():
    if not isinstance(methods, dict):
        continue
    for _method, op in methods.items():
        if isinstance(op, dict) and "security" in op:
            op["security"] = dedupe_security(op["security"])

ordered = sort_obj(data)
# Prefer conventional top-level key order while keeping nested keys sorted.
preferred = [
    "openapi",
    "info",
    "servers",
    "security",
    "tags",
    "paths",
    "components",
]
top = {k: ordered[k] for k in preferred if k in ordered}
for k, v in ordered.items():
    if k not in top:
        top[k] = v

dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(
    yaml.dump(
        top,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    ),
    encoding="utf-8",
)
print(f"Wrote {dest}")
PY

if [[ "$VALIDATE_ONLY" -eq 1 ]]; then
  # Regenerate to temp path only for validation side-effect already done;
  # --validate-only still refreshes the committed file for local consistency.
  :
fi
