"""Architecture tests for eSIM lifecycle sole-mutator (ADR 014)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

_ALLOWED_STATUS_WRITERS = frozenset(
    {
        "src/apps/esims/services/lifecycle_service.py",
    }
)


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "migrations" not in p.parts)


def _is_esim_objects_create(func: ast.AST) -> bool:
    """True for Esim.objects.create(...)."""
    if not isinstance(func, ast.Attribute) or func.attr != "create":
        return False
    objects = func.value
    if not isinstance(objects, ast.Attribute) or objects.attr != "objects":
        return False
    return isinstance(objects.value, ast.Name) and objects.value.id == "Esim"


def _collect_esim_status_writes(path: Path) -> list[str]:
    rel = str(path.relative_to(SRC_ROOT.parent))
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "esim"
                ):
                    offenders.append(f"{rel}:{target.lineno} esim.status =")
        elif isinstance(node, ast.Call) and _is_esim_objects_create(node.func):
            for kw in node.keywords:
                if kw.arg == "status":
                    offenders.append(
                        f"{rel}:{node.lineno} Esim.objects.create(status=)"
                    )
    return offenders


def test_esim_status_mutations_only_in_lifecycle_service() -> None:
    """Only LifecycleService may write Esim.status."""
    offenders: list[str] = []
    for path in _iter_python_files(SRC_ROOT):
        rel = str(path.relative_to(SRC_ROOT.parent))
        if rel in _ALLOWED_STATUS_WRITERS:
            continue
        offenders.extend(_collect_esim_status_writes(path))
    assert (
        offenders == []
    ), f"Esim.status must only be written via LifecycleService: {offenders}"
