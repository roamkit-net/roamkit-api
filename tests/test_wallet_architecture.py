"""Architecture guards for apps.wallet (ADR 017 — Wallet is not Billing)."""

from __future__ import annotations

import ast
from pathlib import Path

WALLET_ROOT = Path(__file__).resolve().parents[1] / "src" / "apps" / "wallet"
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# Cap 3 Credit Conversion is the only wallet module allowed to call CreditService.
_CREDIT_ALLOWED = frozenset({"conversion.py"})


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "migrations" not in p.parts)


def _rel(path: Path, lineno: int) -> str:
    return f"{path.relative_to(SRC_ROOT.parent)}:{lineno}"


def test_wallet_modules_do_not_import_credit_service_except_conversion() -> None:
    offenders: list[str] = []
    for path in _iter_python_files(WALLET_ROOT):
        if path.name in _CREDIT_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            module = node.module
            if "billing" in module and "credit" in module:
                offenders.append(_rel(path, node.lineno))
    assert not offenders, "wallet must not import CreditService:\n" + "\n".join(
        offenders
    )


def test_wallet_modules_do_not_assign_account_balance() -> None:
    offenders: list[str] = []
    for path in _iter_python_files(WALLET_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "balance":
                        offenders.append(_rel(path, target.lineno))
    assert not offenders, "wallet must not mutate balance:\n" + "\n".join(offenders)


def test_confirmation_and_observation_do_not_import_billing_credit() -> None:
    for name in ("confirmation.py", "observation.py", "allocation.py", "hd.py"):
        path = WALLET_ROOT / "services" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            assert "credit" not in node.module or "billing" not in node.module
            for alias in node.names:
                assert alias.name != "CreditService"
                assert alias.name != "credit_service"
