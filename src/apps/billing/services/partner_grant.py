"""Partner credit grant (ADR 023). No HTTP surface."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

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

logger = logging.getLogger(__name__)

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
    """Team balance is already negative. The cache is left unchanged."""


class PartnerGrantService:
    """Move credits from a channel team Account to an attributed customer.

    Lock order inside one transaction: ``CustomerAttribution``, team Account,
    personal Account, then ``CreditService.debit``, ``CreditService.credit``,
    then the grant insert. ``IntegrityError`` on that insert is handled only
    after the atomic has rolled back.
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

        existing = self._existing(partner_channel, idempotency_key)
        if existing is not None:
            return self._matching(existing, customer, quantized)

        personal = ensure_billing_account(customer)
        team_account_id = partner_channel.organization.account_id
        try:
            with transaction.atomic():
                attribution = self._lock_attribution(customer)
                existing = self._existing(partner_channel, idempotency_key)
                if existing is not None:
                    return self._matching(existing, customer, quantized)
                self._require_current_channel(attribution, partner_channel)
                team = Account.objects.select_for_update().get(pk=team_account_id)
                if team.balance < 0:
                    logger.error(
                        "partner_grant partner_channel_id=%s customer_user_id=%s "
                        "team_account_id=%s balance=%s reason=%s",
                        partner_channel.pk,
                        customer.pk,
                        team.pk,
                        team.balance,
                        "partner_grant.negative_balance",
                    )
                    raise PartnerGrantNegativeBalance(
                        "Team account balance is already negative"
                    )
                Account.objects.select_for_update().get(pk=personal.pk)
                grant_id = uuid.uuid4()
                debit = self._credits.debit(
                    team,
                    quantized,
                    reference_type=LedgerReferenceType.PARTNER_GRANT_OUT,
                    reference_id=str(grant_id),
                    idempotency_key=f"partner-grant-out:{grant_id}",
                )
                credit = self._credits.credit(
                    personal,
                    quantized,
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
                    amount=quantized,
                    idempotency_key=idempotency_key,
                    debit_ledger_entry=debit,
                    credit_ledger_entry=credit,
                )
        except IntegrityError:
            raced = self._existing(partner_channel, idempotency_key)
            if raced is None:
                raise
            return self._matching(raced, customer, quantized)
        if request_id is not None:
            _audit_created(created, request_id)
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


def _audit_created(grant: PartnerCreditGrant, request_id: str) -> None:
    """HTTP audit after the grant transaction has committed."""
    created_at = grant.created_at.isoformat().replace("+00:00", "Z")
    logger.info(
        "partner_grant.created actor_user_id=%s partner_channel_id=%s "
        "organization_id=%s action=%s created_at=%s request_id=%s "
        "target_customer_id=%s amount=%s",
        grant.granted_by_user_id_snapshot,
        grant.partner_channel_id,
        grant.partner_channel.organization_id,
        "partner_grant.created",
        created_at,
        request_id,
        grant.customer_user_id_snapshot,
        f"{grant.amount:.6f}",
    )


partner_grant_service = PartnerGrantService()
