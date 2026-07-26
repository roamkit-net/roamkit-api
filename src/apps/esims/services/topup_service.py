"""Top-up listing and prepaid purchase (ADR-010)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.billing.exceptions import BillingDisabledError
from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service, ensure_billing_account
from apps.esims.exceptions import TopupPackageNotFoundError
from apps.esims.models import Topup
from apps.orders.exceptions import (
    IdempotencyKeyRequiredError,
    ProviderFulfillmentError,
    SpendInProgressError,
)
from shared.events.billing_events import (
    CreditDebited,
    CreditGranted,
    FulfillmentRefunded,
)
from shared.events.event_bus import event_bus
from shared.events.order_events import TopupCompleted

if TYPE_CHECKING:
    from decimal import Decimal

    from apps.billing.models import Account, CreditLedgerEntry
    from apps.esims.models import Esim
    from shared.providers.esim import TopupPackage, TopupProvider, TopupResult

logger = logging.getLogger(__name__)


class TopupService:
    """Lists and purchases top-up packages via TopupProvider + CreditService."""

    def __init__(self, provider: TopupProvider) -> None:
        self.provider = provider

    def list_topups(self, esim: Esim) -> list[TopupPackage]:
        """Return purchasable top-up packages for ``esim``."""
        return self.provider.list_topups(esim.iccid)

    def purchase(
        self,
        esim: Esim,
        *,
        package_id: str,
        idempotency_key: str,
    ) -> Topup:
        """Debit credits, reserve Topup, then submit via provider.

        Price is taken from the provider catalog (never from the client).
        Provider failure → compensating REFUND + FAILED + snapshot events.
        Idempotent on ``idempotency_key`` like deposits / orders.
        """
        if not idempotency_key:
            raise IdempotencyKeyRequiredError("idempotency_key is required")

        package = self._resolve_package(esim, package_id)
        account = ensure_billing_account(esim.user)
        amount = package.price_usd

        topup, debit_entry, replay = self._reserve_and_debit(
            account=account,
            esim=esim,
            package_external_id=package.external_id,
            amount=amount,
            idempotency_key=idempotency_key,
        )
        if replay:
            return topup

        event_bus.publish(
            CreditDebited(
                account_id=str(account.pk),
                amount=amount,
                balance_after=debit_entry.balance_after,
                reference_type=LedgerReferenceType.TOPUP,
                reference_id=str(topup.pk),
                ledger_entry_id=str(debit_entry.pk),
                created_at=debit_entry.created_at,
            )
        )

        try:
            result = self.provider.submit_topup(esim.iccid, package.external_id)
        except Exception as exc:
            self._compensate_failed(topup=topup, account=account, amount=amount)
            logger.exception("Topup %s provider fulfillment failed", topup.pk)
            raise ProviderFulfillmentError("Provider fulfillment failed") from exc

        return self._persist_fulfillment(
            topup=topup,
            result=result,
            amount=amount,
            debit_entry=debit_entry,
        )

    def _resolve_package(self, esim: Esim, package_id: str) -> TopupPackage:
        packages = self.list_topups(esim)
        for package in packages:
            if package.external_id == package_id:
                return package
        raise TopupPackageNotFoundError(
            f"Top-up package {package_id!r} is not available for this eSIM"
        )

    def _reserve_and_debit(
        self,
        *,
        account: Account,
        esim: Esim,
        package_external_id: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> tuple[Topup, CreditLedgerEntry | None, bool]:
        if not settings.BILLING_ENABLED:
            raise BillingDisabledError("Billing is disabled")

        with transaction.atomic():
            existing = (
                Topup.objects.select_for_update()
                .filter(idempotency_key=idempotency_key)
                .first()
            )
            if existing is not None:
                if existing.status == Topup.Status.FULFILLING:
                    raise SpendInProgressError("Top-up request is still in progress")
                return existing, None, True

            try:
                topup = Topup.objects.create(
                    account=account,
                    esim=esim,
                    package_external_id=package_external_id,
                    amount=amount,
                    status=Topup.Status.FULFILLING,
                    idempotency_key=idempotency_key,
                )
            except IntegrityError:
                existing = Topup.objects.filter(idempotency_key=idempotency_key).first()
                if existing is None:
                    raise
                if existing.status == Topup.Status.FULFILLING:
                    raise SpendInProgressError(
                        "Top-up request is still in progress"
                    ) from None
                return existing, None, True

            entry = credit_service.debit(
                account,
                amount,
                reference_type=LedgerReferenceType.TOPUP,
                reference_id=str(topup.pk),
                idempotency_key=f"topup-debit:{topup.pk}",
            )
            return topup, entry, False

    def _compensate_failed(
        self,
        *,
        topup: Topup,
        account: Account,
        amount: Decimal,
    ) -> None:
        events: list[CreditGranted | FulfillmentRefunded] = []
        with transaction.atomic():
            locked = Topup.objects.select_for_update().get(pk=topup.pk)
            if locked.status == Topup.Status.FAILED:
                return
            locked.status = Topup.Status.FAILED
            locked.save(update_fields=["status", "updated_at"])

            entry = credit_service.credit(
                account,
                amount,
                reference_type=LedgerReferenceType.REFUND,
                reference_id=str(locked.pk),
                idempotency_key=f"topup-refund:{locked.pk}",
            )
            events.append(
                CreditGranted(
                    account_id=str(account.pk),
                    amount=amount,
                    balance_after=entry.balance_after,
                    reference_type=LedgerReferenceType.REFUND,
                    reference_id=str(locked.pk),
                    ledger_entry_id=str(entry.pk),
                    created_at=entry.created_at,
                )
            )
            events.append(
                FulfillmentRefunded(
                    account_id=str(account.pk),
                    amount=amount,
                    balance_after=entry.balance_after,
                    reference_type=LedgerReferenceType.TOPUP,
                    reference_id=str(locked.pk),
                    ledger_entry_id=str(entry.pk),
                    reason="provider_fulfillment_failed",
                    created_at=entry.created_at,
                )
            )

        for event in events:
            event_bus.publish(event)

    def _persist_fulfillment(
        self,
        *,
        topup: Topup,
        result: TopupResult,
        amount: Decimal,
        debit_entry: CreditLedgerEntry,
    ) -> Topup:
        with transaction.atomic():
            topup.external_order_id = result.external_order_id
            topup.status = Topup.Status.FULFILLED
            topup.save(update_fields=["external_order_id", "status", "updated_at"])

        event_bus.publish(
            TopupCompleted(
                topup_id=str(topup.pk),
                esim_id=str(topup.esim_id),
                account_id=str(topup.account_id),
                package_id=topup.package_external_id,
                amount=amount,
                external_order_id=result.external_order_id,
                balance_after=debit_entry.balance_after,
                ledger_entry_id=str(debit_entry.pk),
                created_at=topup.updated_at,
            )
        )
        return topup
