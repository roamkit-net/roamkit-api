"""Shared pytest fixtures for roamkit-api."""

from __future__ import annotations

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _auth_throttle_headroom(settings) -> None:
    """
    Raise auth throttle ceilings for the test suite.

    Production defaults (e.g. 5 register/hour) would flake across test_auth_api.
    Individual throttle tests override REST_FRAMEWORK rates explicitly.
    """
    rates = {
        "auth_token": "1000/min",
        "auth_register": "1000/hour",
        "auth_password_reset": "1000/hour",
        "auth_activate": "1000/hour",
        "auth_password_reset_confirm": "1000/hour",
        "auth_google": "1000/min",
        "billing_voucher_redeem": "1000/min",
    }
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}),
            **rates,
        },
    }
    cache.clear()
