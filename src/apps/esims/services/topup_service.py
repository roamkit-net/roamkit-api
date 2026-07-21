"""Top-up listing for owned eSIMs (purchase is Phase 3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.esims.models import Esim
    from shared.providers.esim import TopupPackage, TopupProvider


class TopupService:
    """Lists available top-up packages for an eSIM via TopupProvider."""

    def __init__(self, provider: TopupProvider) -> None:
        self.provider = provider

    def list_topups(self, esim: Esim) -> list[TopupPackage]:
        """Return purchasable top-up packages for ``esim`` (read-only in Phase 2)."""
        return self.provider.list_topups(esim.iccid)
