"""Resolve active ICCID from a UEM device record (ADR 021 staging proof).

Read-only. No DeviceBinding / Esim mutations.
"""

from __future__ import annotations

from typing import Any

from apps.organizations.exceptions import UemInventoryUnavailableError


def resolve_top_level_iccid(device: dict[str, Any]) -> str:
    """Return validated top-level ICCID or raise ``UemInventoryUnavailableError``.

    Rules (staging proof lock):

    * blank/null top-level ``iccid`` → unavailable
    * ``sims == []`` → unavailable (observed stale inventory shape)
    * when ``sims`` is a non-empty list, top-level ICCID must appear in it
    """
    if not isinstance(device, dict):
        raise UemInventoryUnavailableError("UEM device record unavailable")

    iccid = str(device.get("iccid") or "").strip()
    if not iccid:
        raise UemInventoryUnavailableError("UEM telephony inventory unavailable")

    sims = device.get("sims")
    if sims is None:
        return iccid
    if not isinstance(sims, list):
        raise UemInventoryUnavailableError("UEM telephony inventory unavailable")
    if len(sims) == 0:
        raise UemInventoryUnavailableError("UEM telephony inventory unavailable")

    sim_iccids = {
        str(sim.get("iccid") or "").strip() for sim in sims if isinstance(sim, dict)
    }
    sim_iccids.discard("")
    if iccid not in sim_iccids:
        raise UemInventoryUnavailableError(
            "UEM top-level ICCID not present in sims inventory"
        )
    return iccid
