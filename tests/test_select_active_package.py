"""select_active_package — first status == active wins; others never win."""

from __future__ import annotations

from types import SimpleNamespace

from apps.organizations.services.device_status import select_active_package


def _row(package_id: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(id=package_id, status=status)


def test_empty_results_is_null() -> None:
    assert select_active_package([]) is None


def test_only_not_active_is_null() -> None:
    assert select_active_package([_row("1", "not_active")]) is None


def test_simple_combo_picks_active_topup() -> None:
    rows = [
        _row("esim-300mb", "expired"),
        _row("topup-1gb-active", "active"),
        _row("topup-1gb-queued", "queued"),
    ]
    picked = select_active_package(rows)
    assert picked is not None
    assert picked.id == "topup-1gb-active"


def test_full_combined_first_active_in_provider_order_wins() -> None:
    rows = [
        _row("active-first", "active"),
        _row("not-active", "not_active"),
        _row("queued", "queued"),
        _row("expired", "expired"),
        _row("active-second", "active"),
    ]
    picked = select_active_package(rows)
    assert picked is not None
    assert picked.id == "active-first"


def test_expired_finished_unknown_never_win() -> None:
    rows = [
        _row("1", "expired"),
        _row("2", "finished"),
        _row("3", "unknown"),
        _row("4", "ACTIVE"),
    ]
    assert select_active_package(rows) is None
