"""Use the restored+migrated smoke DB; do not create/drop test_* databases."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings() -> None:
    """Keep connections on the smoke database name (no test_ prefix)."""
    from django.conf import settings

    name = settings.DATABASES["default"]["NAME"]
    settings.DATABASES["default"].setdefault("TEST", {})
    settings.DATABASES["default"]["TEST"]["NAME"] = name


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    """DB is prepared by scripts/run_migration_smoke.sh."""
    if os.environ.get("MIGRATION_SMOKE") != "1":
        raise RuntimeError(
            "tests/migration_smoke requires MIGRATION_SMOKE=1 "
            "(run via scripts/run_migration_smoke.sh)"
        )
    with django_db_blocker.unblock():
        yield
