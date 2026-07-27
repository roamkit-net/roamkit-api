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
POLYGON_ROOT = SRC_ROOT / "apps" / "integrations" / "polygon"

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

_VOUCHER_MODEL_NAMES = frozenset(
    {
        "Voucher",
        "VoucherCampaign",
        "VoucherBatch",
        "VoucherRedemption",
    }
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
        offenders.extend(_collect_billing_imports(path))
    assert offenders == []


def test_polygon_provider_does_not_import_billing_money_path() -> None:
    """BlockchainProvider must stay read-only wrt credits / ledger / Account."""
    offenders: list[str] = []
    for path in _iter_python_files(POLYGON_ROOT):
        offenders.extend(_collect_billing_imports(path))
    assert (
        offenders == []
    ), "Polygon provider must not import billing money models or CreditService"


def _collect_billing_imports(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []
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
    return offenders


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
    assert (
        offenders == []
    ), "Modules outside apps.billing must not write Account.balance"


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


# --- PR0.5 readiness gates (go-live must-have #1 extensions) ---

_MONEY_PATH_ROOTS = (
    SRC_ROOT / "apps" / "billing",
    SRC_ROOT / "apps" / "orders" / "services",
    SRC_ROOT / "apps" / "esims" / "services",
    SRC_ROOT / "apps" / "integrations" / "polygon",
    SRC_ROOT / "apps" / "integrations" / "airalo",
)

_FORBIDDEN_FRONTEND_IMPORT_PREFIXES = (
    "next",
    "react",
    "react-dom",
    "@/",  # Next app alias — must never appear in Django source
)


def test_money_path_has_no_todo_or_fixme() -> None:
    """Scoped gate: unfinished work markers must not ship on the money path."""
    offenders: list[str] = []
    for root in _MONEY_PATH_ROOTS:
        if not root.exists():
            continue
        for path in _iter_python_files(root):
            rel = str(path.relative_to(SRC_ROOT.parent))
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                # Match whole-word TODO/FIXME in comments or strings used as markers.
                upper = line.upper()
                if "TODO" in upper or "FIXME" in upper:
                    # Allow words like "todolist" false positives only if not markers.
                    if (
                        "TODO" in line
                        or "FIXME" in line
                        or "todo:" in line.lower()
                        or "fixme:" in line.lower()
                    ):
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], "Remove TODO/FIXME from money-path modules before go-live"


def test_billing_does_not_import_frontend_packages() -> None:
    """Billing (and Django src) must not depend on Next/React client packages."""
    billing_root = SRC_ROOT / "apps" / "billing"
    offenders: list[str] = []
    for path in _iter_python_files(billing_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names = [node.module]
            for name in names:
                if any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in _FORBIDDEN_FRONTEND_IMPORT_PREFIXES
                    if not prefix.startswith("@")
                ):
                    offenders.append(f"{path}:{node.lineno} import {name}")
    assert offenders == [], "apps.billing must not import frontend packages"


def test_orders_and_esims_do_not_bypass_credit_service_for_balance() -> None:
    """Spend apps may call CreditService; they must not write Account.balance."""
    offenders: list[str] = []
    for root in (
        SRC_ROOT / "apps" / "orders",
        SRC_ROOT / "apps" / "esims",
    ):
        for path in _iter_python_files(root):
            offenders.extend(_collect_balance_writes(path))
    assert (
        offenders == []
    ), "orders/esims must debit/credit only via CreditService (no .balance writes)"


def test_non_billing_apps_do_not_import_voucher_models() -> None:
    """Voucher* models stay inside apps.billing (ADR 011 / 012)."""
    offenders: list[str] = []
    for root in _NON_BILLING_ROOTS:
        if not root.exists():
            continue
        for path in _iter_python_files(root):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("apps.billing"):
                        for alias in node.names:
                            if alias.name in _VOUCHER_MODEL_NAMES or (
                                alias.name and alias.name.startswith("Voucher")
                            ):
                                offenders.append(
                                    f"{path}:{node.lineno} from {module} "
                                    f"import {alias.name}"
                                )
    assert offenders == [], "Non-billing modules must not import Voucher* models"


def test_only_voucher_redeem_service_uses_voucher_reference_type() -> None:
    """LedgerReferenceType.VOUCHER credits must originate from voucher_redeem."""
    allowed = frozenset(
        {
            "src/apps/billing/services/voucher_redeem.py",
            "src/apps/billing/models.py",
        }
    )
    offenders: list[str] = []
    for path in _iter_python_files(SRC_ROOT):
        rel = str(path.relative_to(SRC_ROOT.parent))
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if (
            "LedgerReferenceType.VOUCHER" not in text
            and 'VOUCHER = "voucher"' not in text
        ):
            if '"voucher"' not in text and "'voucher'" not in text:
                continue
        if "LedgerReferenceType.VOUCHER" in text and rel not in allowed:
            # Allow tests and REFERENCE_MODELS registration in models.
            if rel.startswith("tests/"):
                continue
            if rel.endswith("models.py") and "REFERENCE_MODELS" in text:
                continue
            offenders.append(rel)
    assert offenders == [], (
        "Only voucher_redeem service may use LedgerReferenceType.VOUCHER "
        f"for money path: {offenders}"
    )
