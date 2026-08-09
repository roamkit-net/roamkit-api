"""UEM top-level ICCID resolve rules (ADR 021 staging proof)."""

from __future__ import annotations

import pytest

from apps.organizations.exceptions import UemInventoryUnavailableError
from apps.organizations.services.uem_iccid import resolve_top_level_iccid


def test_resolve_accepts_top_level_in_sims():
    iccid = resolve_top_level_iccid(
        {
            "iccid": "8900424101001825931",
            "sims": [
                {"iccid": "8900424101001825931", "homeCarrier": "A1 HR"},
            ],
        }
    )
    assert iccid == "8900424101001825931"


def test_resolve_accepts_top_level_when_sims_key_absent():
    assert (
        resolve_top_level_iccid({"iccid": "89852350326100304891"})
        == "89852350326100304891"
    )


def test_resolve_rejects_null_iccid():
    with pytest.raises(UemInventoryUnavailableError):
        resolve_top_level_iccid({"iccid": None, "sims": []})


def test_resolve_rejects_empty_sims_list():
    with pytest.raises(UemInventoryUnavailableError):
        resolve_top_level_iccid(
            {"iccid": "8900424101001825931", "sims": []},
        )


def test_resolve_rejects_top_level_not_in_sims():
    with pytest.raises(UemInventoryUnavailableError):
        resolve_top_level_iccid(
            {
                "iccid": "111",
                "sims": [
                    {"iccid": "222"},
                    {"iccid": "333"},
                ],
            }
        )


def test_resolve_dual_sim_top_level_second_entry():
    iccid = resolve_top_level_iccid(
        {
            "iccid": "89103000000326385396",
            "sims": [
                {"iccid": "8948010000036347169"},
                {"iccid": "89103000000326385396"},
            ],
        }
    )
    assert iccid == "89103000000326385396"
