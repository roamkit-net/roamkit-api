"""Partner credit grant (ADR 023 / ADR 024). No HTTP surface."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.billing.models import Account, LedgerReferenceType
from apps.billing.partner_channel import CustomerAttribution, PartnerCreditGrant
from apps.billing.services.account import ensure_billing_account
from apps.billing.services.credit import CreditService, credit_service
from apps.organizations.models import Membership, MembershipRole, MembershipStatus

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.billing.partner_channel import PartnerChannel

_GRANT_ROLES = frozenset({MembershipRole.OWNER, MembershipRole.ADMIN})


class PartnerGrantError(Exception):
    """Grant was refused before any ledger write."""


class PartnerChannelDisabled(PartnerGrantError):
    """PARTNER_CHANNEL_ENABLED is off."""


class PartnerGrantForbidden(PartnerGrantError):
    """Actor has no active owner or admin membership on this channel."""


class CustomerNotAttributed(PartnerGrantError):
    """The customer has no CustomerAttribution row."""


class CustomerAttributionChanged(PartnerGrantError):
    """Under lock, the customer is no longer on this channel."""


class PartnerGrantIdempotencyConflict(PartnerGrantError):
    """The key exists for a different customer or amount."""


class PartnerGrantNegativeBalance(PartnerGrantError):
    """Settlement balance is already negative. The cache is left unchanged."""


class PartnerGrantSameAccount(PartnerGrantError):
    """Source and destination resolved to one Account."""


class PartnerGrantService:
    """Move credits from a settlement Account to an attributed customer.

    ``grant`` is the legacy team wrapper: it checks membership and reads
    ``organization.account_id``. ``grant_from_account`` takes an Account the
    caller already resolved. Both call ``_execute``.

    An exact ``(partner_channel, idempotency_key)`` replay returns before
    attribution is evaluated, so a later attribution change does not make a
    successful grant unreplayable.

    Inside the transaction the current ``CustomerAttribution`` is locked
    first. The destination personal Account is created there, so a later
    failure rolls it back. The two Account rows are then locked in primary-key
    order. ``CreditService`` locks each Account again in this same transaction;
    those calls re-lock rows already held here and do not open a new
    lock-order race. ``CreditService`` remains the only balance writer.
    ``IntegrityError`` on the grant insert is handled only after the atomic
    has rolled back.
    """

    def __init__(self, *, credits: CreditService | None = None) -> None:
        self._credits = credits or credit_service

    def grant(
        self,
        *,
        actor: User,
        partner_channel: PartnerChannel,
        customer: User,
        amount: Decimal | int | str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> PartnerCreditGrant:
        if not idempotency_key:
            raise PartnerGrantError("idempotency_key is required")
        quantized = CreditService._normalize_amount(amount)
        if not settings.PARTNER_CHANNEL_ENABLED:
            raise PartnerChannelDisabled("Partner channel is disabled")
        self._require_grant_membership(actor, partner_channel)
        return self._execute(
            actor=actor,
            partner_channel=partner_channel,
            source_account_id=partner_channel.organization.account_id,
            customer=customer,
            amount=quantized,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def grant_from_account(
        self,
        *,
        actor: User,
        partner_channel: PartnerChannel,
        source_account: Account,
        customer: User,
        amount: Decimal | int | str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> PartnerCreditGrant:
        """Grant from a settlement Account the caller already resolved.

        Does not read membership or the channel's owner relation. The HTTP
        caller checks ``partner_role_can_grant`` and
        ``resolve_partner_settlement_account`` first.
        """
        if not idempotency_key:
            raise PartnerGrantError("idempotency_key is required")
        quantized = CreditService._normalize_amount(amount)
        if not settings.PARTNER_CHANNEL_ENABLED:
            raise PartnerChannelDisabled("Partner channel is disabled")
        return self._execute(
            actor=actor,
            partner_channel=partner_channel,
            source_account_id=source_account.pk,
            customer=customer,
            amount=quantized,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def _execute(
        self,
        *,
        actor: User,
        partner_channel: PartnerChannel,
        source_account_id: UUID,
        customer: User,
        amount: Decimal,
        idempotency_key: str,
        request_id: str | None,
    ) -> PartnerCreditGrant:
        existing = self._existing(partner_channel, idempotency_key)
        if existing is not None:
            _log_grant("partner.grant.replay", partner_channel, existing, customer)
            return self._matching(existing, customer, amount)

        try:
            with transaction.atomic():
                attribution = self._lock_attribution(customer)
                existing = self._existing(partner_channel, idempotency_key)
                if existing is not None:
                    _log_grant(
                        "partner.grant.replay",
                        partner_channel,
                        existing,
                        customer,
                    )
                    return self._matching(existing, customer, amount)
                attribution = self._require_current_channel(
                    attribution,
                    partner_channel,
                )
                destination = ensure_billing_account(customer)
                source, destination = self._lock_ordered_accounts(
                    source_account_id,
                    destination.pk,
                )
                if source.balance < 0:
                    _log_grant(
                        "partner.grant.insufficient_funds",
                        partner_channel,
                        None,
                        customer,
                    )
                    raise PartnerGrantNegativeBalance(
                        "Settlement account balance is already negative"
                    )
                grant_id = uuid.uuid4()
                debit = self._credits.debit(
                    source,
                    amount,
                    reference_type=LedgerReferenceType.PARTNER_GRANT_OUT,
                    reference_id=str(grant_id),
                    idempotency_key=f"partner-grant-out:{grant_id}",
                )
                credit = self._credits.credit(
                    destination,
                    amount,
                    reference_type=LedgerReferenceType.PARTNER_GRANT_IN,
                    reference_id=str(grant_id),
                    idempotency_key=f"partner-grant-in:{grant_id}",
                )
                created = PartnerCreditGrant.objects.create(
                    id=grant_id,
                    partner_channel=partner_channel,
                    customer_user=customer,
                    customer_user_id_snapshot=customer.pk,
                    customer_attribution=attribution,
                    granted_by=actor,
                    granted_by_user_id_snapshot=actor.pk,
                    amount=amount,
                    idempotency_key=idempotency_key,
                    debit_ledger_entry=debit,
                    credit_ledger_entry=credit,
                )
        except IntegrityError:
            raced = self._existing(partner_channel, idempotency_key)
            if raced is None:
                raise
            _log_grant("partner.grant.replay", partner_channel, raced, customer)
            return self._matching(raced, customer, amount)
        _log_grant(
            "partner.grant.succeeded",
            partner_channel,
            created,
            customer,
            request_id=request_id,
        )
        return created

    @staticmethod
    def _require_grant_membership(actor: User, partner_channel: PartnerChannel) -> None:
        membership = Membership.objects.filter(
            organization_id=partner_channel.organization_id,
            user_id=actor.pk,
            status=MembershipStatus.ACTIVE,
            role__in=_GRANT_ROLES,
        ).first()
        if membership is None:
            raise PartnerGrantForbidden(
                "Grant requires an active owner or admin membership"
            )

    @staticmethod
    def _lock_attribution(customer: User) -> CustomerAttribution | None:
        return (
            CustomerAttribution.objects.select_for_update()
            .filter(user_id=customer.pk)
            .first()
        )

    @staticmethod
    def _require_current_channel(
        attribution: CustomerAttribution | None,
        partner_channel: PartnerChannel,
    ) -> CustomerAttribution:
        if attribution is None:
            raise CustomerNotAttributed("Customer has no partner attribution")
        if attribution.partner_channel_id != partner_channel.pk:
            raise CustomerAttributionChanged(
                "Customer is no longer attributed to this channel"
            )
        return attribution

    @staticmethod
    def _lock_ordered_accounts(
        source_account_id: UUID,
        destination_account_id: UUID,
    ) -> tuple[Account, Account]:
        """Lock both Accounts in one primary-key order.

        Opposite grants (A to B, and B to A) take the same lock sequence.
        ``CreditService`` may ``select_for_update`` those rows again later in
        this transaction; PostgreSQL already holds them, so that does not
        reorder the locks.
        """
        if source_account_id == destination_account_id:
            raise PartnerGrantSameAccount(
                "Grant source and destination accounts are the same"
            )
        ordered_ids = sorted((source_account_id, destination_account_id))
        locked = {
            row.pk: row
            for row in Account.objects.select_for_update()
            .filter(pk__in=ordered_ids)
            .order_by("pk")
        }
        try:
            return locked[source_account_id], locked[destination_account_id]
        except KeyError as exc:
            raise Account.DoesNotExist("Grant account row is missing") from exc

    @staticmethod
    def _existing(
        partner_channel: PartnerChannel,
        idempotency_key: str,
    ) -> PartnerCreditGrant | None:
        return PartnerCreditGrant.objects.filter(
            partner_channel=partner_channel,
            idempotency_key=idempotency_key,
        ).first()

    @staticmethod
    def _matching(
        grant: PartnerCreditGrant,
        customer: User,
        amount: Decimal,
    ) -> PartnerCreditGrant:
        if grant.customer_user_id_snapshot != customer.pk or grant.amount != amount:
            raise PartnerGrantIdempotencyConflict(
                "idempotency_key was already used for a different grant"
            )
        return grant


def _log_grant(
    event: str,
    partner_channel: PartnerChannel,
    grant: PartnerCreditGrant | None,
    customer: User,
    *,
    request_id: str | None = None,
) -> None:
    from apps.billing.services.partner_log import log_partner_event

    identity = None
    if grant is not None and grant.idempotency_key:
        identity = hashlib.sha256(grant.idempotency_key.encode()).hexdigest()[:16]
    log_partner_event(
        event,
        partner_channel_id=partner_channel.pk,
        kind=partner_channel.kind,
        grant_id=None if grant is None else grant.pk,
        customer_user_id=customer.pk,
        idempotency_id=identity,
        request_id=request_id,
    )


partner_grant_service = PartnerGrantService()
