"""Architecture tests for billing money-path invariants (ADR-010)."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from apps.accounts.models import User
from apps.billing.models import (
    AppendOnlyViolation,
    CreditLedgerEntry,
    LedgerReferenceType,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
AIRALO_ROOT = SRC_ROOT / "apps" / "integrations" / "airalo"

# Only CreditService may mutate Account.balance after create.
_ALLOWED_BALANCE_WRITERS = frozenset(
    {
        "src/apps/billing/services/credit.py",
    }
)

_NON_BILLING_ROOTS = (
    SRC_ROOT / "apps" / "accounts",
    SRC_ROOT / "apps" / "catalog",
    SRC_ROOT / "apps" / "orders",
    SRC_ROOT / "apps" / "esims",
    SRC_ROOT / "apps" / "integrations",
    SRC_ROOT / "shared",
    SRC_ROOT / "core",
    SRC_ROOT / "config",
)


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "migrations" not in p.parts)


def _collect_balance_writes(path: Path) -> list[str]:
    """Return human-readable offenders for balance mutations in one file."""
    rel = str(path.relative_to(SRC_ROOT.parent))
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "balance":
                    offenders.append(f"{rel}:{target.lineno} assign .balance")
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Attribute) and target.attr == "balance":
                offenders.append(f"{rel}:{target.lineno} augassign .balance")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "update":
                for kw in node.keywords:
                    if kw.arg == "balance":
                        offenders.append(f"{rel}:{node.lineno} update(balance=)")
    return offenders


def test_airalo_modules_do_not_import_billing() -> None:
    offenders: list[str] = []
    for path in _iter_python_files(AIRALO_ROOT):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "apps.billing" or alias.name.startswith(
                        "apps.billing."
                    ):
                        offenders.append(f"{path}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "apps.billing" or module.startswith("apps.billing."):
                    offenders.append(f"{path}:{node.lineno} from {module}")
                if module == "apps" and any(
                    alias.name == "billing" for alias in node.names
                ):
                    offenders.append(f"{path}:{node.lineno} from apps import billing")
    assert offenders == []


def test_account_balance_mutations_only_in_credit_service() -> None:
    """No production module outside CreditService may write Account.balance.

    Scans all of ``src/`` (orders, accounts, catalog, Airalo, other billing
    modules, …) — not only the Airalo package. Only ``services/credit.py``
    is exempt.
    """
    offenders: list[str] = []
    for path in _iter_python_files(SRC_ROOT):
        rel = str(path.relative_to(SRC_ROOT.parent))
        if rel in _ALLOWED_BALANCE_WRITERS:
            continue
        offenders.extend(_collect_balance_writes(path))
    assert offenders == [], "Account.balance must only be written via CreditService"


def test_non_billing_modules_never_write_account_balance() -> None:
    """Explicit guard: apps outside ``apps.billing`` never touch balance."""
    offenders: list[str] = []
    for root in _NON_BILLING_ROOTS:
        if not root.exists():
            continue
        for path in _iter_python_files(root):
            offenders.extend(_collect_balance_writes(path))
    assert offenders == [], (
        "Modules outside apps.billing must not write Account.balance"
    )


@pytest.mark.django_db
def test_ledger_append_only_enforced_on_model() -> None:
    user = User.objects.create_user(email="arch@example.com", password="secret123")
    account = user.billing_account
    entry = CreditLedgerEntry.objects.create(
        account=account,
        delta=Decimal("1.000000"),
        balance_after=Decimal("1.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="arch-1",
        idempotency_key="arch-ledger-1",
    )
    entry.delta = Decimal("2.000000")
    with pytest.raises(AppendOnlyViolation):
        entry.save()
    with pytest.raises(AppendOnlyViolation):
        entry.delete()
