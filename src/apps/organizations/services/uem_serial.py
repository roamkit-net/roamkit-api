"""UEM serialNumber resolve + optional DeviceBinding guid cache refresh.

Match must be exactly one device. Zero → ``DeviceNotFoundError``; multiple →
``DeviceAmbiguousError``. Enrollment helpers may refresh ``uem_device_guid``
after a unique match; serial status/coverage must not call that path.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.integrations.blackberry_uem.client import (
    BlackberryUemClient,
    BlackberryUemClientError,
)
from apps.organizations.exceptions import (
    DeviceAmbiguousError,
    DeviceNotFoundError,
    UemSerialMatchError,
)
from apps.organizations.models import DeviceBinding


def resolve_uem_device_by_serial(
    serial_number: str,
    *,
    client: BlackberryUemClient | None = None,
) -> dict[str, Any]:
    """List UEM devices and return the unique ``serialNumber`` match.

    Side-effect free — does not read or write ``DeviceBinding``.
    """
    serial = (serial_number or "").strip()
    if not serial:
        raise DeviceNotFoundError("Device not found in UEM.")
    if not getattr(settings, "BLACKBERRY_UEM_ENABLED", False):
        raise UemSerialMatchError("BlackBerry UEM integration is disabled")

    uem = client or BlackberryUemClient()
    try:
        return uem.get_device_by_serial(serial)
    except BlackberryUemClientError as exc:
        msg = str(exc)
        if "match count is 0" in msg:
            raise DeviceNotFoundError("Device not found in UEM.") from exc
        if "match count is" in msg:
            raise DeviceAmbiguousError(
                "Multiple UEM devices match this serial."
            ) from exc
        raise UemSerialMatchError(msg) from exc


def refresh_binding_uem_guid_from_serial(
    binding: DeviceBinding,
    *,
    client: BlackberryUemClient | None = None,
) -> dict[str, Any]:
    """Resolve binding serial in UEM; refresh guid cache on unique success.

    Enrollment/admin helper only — not used by serial status/coverage.
    Does not mutate ``esim`` or other binding fields. Fail-closed on 0/>1 match
    — no partial guid update.
    """
    serial = (binding.uem_serial_number or "").strip()
    if not serial:
        raise UemSerialMatchError("DeviceBinding.uem_serial_number is empty")

    try:
        device = resolve_uem_device_by_serial(serial, client=client)
    except (DeviceNotFoundError, DeviceAmbiguousError) as exc:
        raise UemSerialMatchError(str(exc)) from exc
    guid = str(device.get("guid") or "").strip()
    if not guid:
        raise UemSerialMatchError("UEM device match missing guid")

    if binding.uem_device_guid != guid:
        binding.uem_device_guid = guid
        binding.save(update_fields=["uem_device_guid", "updated_at"])
    return device
