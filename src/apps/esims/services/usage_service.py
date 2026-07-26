"""Usage lookup and cache updates for owned eSIMs."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from shared.providers.esim import UsageDTO

if TYPE_CHECKING:
    from apps.esims.models import Esim
    from shared.providers.esim import TopupProvider


class UsageService:
    """Fetches live usage via TopupProvider and refreshes Esim cache fields."""

    def __init__(self, provider: TopupProvider) -> None:
        self.provider = provider

    def get_usage(self, esim: Esim) -> UsageDTO:
        """Return provider usage for ``esim`` and persist the cache snapshot."""
        usage = self.provider.get_usage(esim.iccid)
        self._update_cache(esim, usage)
        from apps.esims.services.lifecycle_service import lifecycle_service

        lifecycle_service.apply_provider_usage(esim, usage)
        return usage

    def _update_cache(self, esim: Esim, usage: UsageDTO) -> None:
        esim.usage_remaining_mb = usage.remaining_mb
        esim.usage_total_mb = usage.total_mb
        esim.usage_status = usage.status
        esim.usage_is_unlimited = usage.is_unlimited
        esim.usage_expired_at = self._parse_expired_at(usage.expired_at)
        esim.usage_synced_at = timezone.now()
        esim.save(
            update_fields=[
                "usage_remaining_mb",
                "usage_total_mb",
                "usage_status",
                "usage_is_unlimited",
                "usage_expired_at",
                "usage_synced_at",
                "updated_at",
            ]
        )

    @staticmethod
    def _parse_expired_at(value: str | None) -> datetime | None:
        if not value:
            return None
        normalized = value.strip()
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        parsed = parse_datetime(normalized)
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed)
        return parsed
