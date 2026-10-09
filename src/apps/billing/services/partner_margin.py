"""Partner margin accrual (ADR 023). Order, topup, and subscription hooks."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services.credit import MONEY_QUANT, CreditService, credit_service

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = logging.getLogger(__name__)


class PartnerChannelTeamAccountMissing(Exception):
    """The channel's organization has no team Account. Rolls the fulfillment back."""


def build_source_id(
    *,
    source_type: str,
    source_uuid: uuid.UUID | int,
    billing_date: date | None = None,
) -> str:
    """Canonical idempotency identity for one commercial event.

    UUID primary keys are lowercase canonical. ``Order.id`` is a bigint, so an
    order source id is that decimal primary key.
    """
    canonical = str(source_uuid)
    if source_type == PartnerMarginAccrual.SourceType.SUBSCRIPTION:
        if billing_date is None:
            raise ValueError("subscription source_id requires billing_date")
        return f"{canonical}:{billing_date.isoformat()}"
    return canonical


class PartnerMarginService:
    """Credit a partner share from a commercial snapshot, or skip."""

    def __init__(self, *, credits: CreditService | None = None) -> None:
        self._credits = credits or credit_service

    def accrue(
        self,
        *,
        source_type: str,
        source_id: str,
        list_price: Decimal | None,
        net_price: Decimal | None,
        customer: User | None,
    ) -> PartnerMarginAccrual | None:
        """Accrue inside the caller's fulfillment transaction.

        A skip returns ``None`` and leaves that transaction to commit.
        A credit or insert failure propagates so the caller rolls back.
        """
        customer_user_id = None if customer is None else str(customer.pk)
        if not settings.PARTNER_CHANNEL_ENABLED:
            self._log(
                source_type=source_type,
                source_id=source_id,
                customer_user_id=customer_user_id,
                partner_channel_id=None,
                organization_id=None,
                reason="partner_margin.flag_disabled",
            )
            return None
        if customer is None:
            self._log(
                source_type=source_type,
                source_id=source_id,
                customer_user_id=None,
                partner_channel_id=None,
                organization_id=None,
                reason="partner_margin.no_attribution",
            )
            return None

        attribution = (
            CustomerAttribution.objects.select_for_update()
            .filter(user_id=customer.pk)
            .first()
        )
        if attribution is None:
            self._log(
                source_type=source_type,
                source_id=source_id,
                customer_user_id=customer_user_id,
                partner_channel_id=None,
                organization_id=None,
                reason="partner_margin.no_attribution",
            )
            return None

        channel = (
            PartnerChannel.objects.select_for_update()
            .select_related("organization__account")
            .get(pk=attribution.partner_channel_id)
        )
        organization_id = str(channel.organization_id)
        partner_channel_id = str(channel.pk)
        existing = PartnerMarginAccrual.objects.filter(
            source_type=source_type,
            source_id=source_id,
        ).first()
        if existing is not None:
            return existing
        if not channel.is_active:
            self._log(
                source_type=source_type,
                source_id=source_id,
                customer_user_id=customer_user_id,
                partner_channel_id=partner_channel_id,
                organization_id=organization_id,
                reason="partner_margin.channel_inactive",
            )
            return None

        skip = self._price_skip(list_price, net_price, channel.revenue_share_percent)
        if skip is not None:
            self._log(
                source_type=source_type,
                source_id=source_id,
                customer_user_id=customer_user_id,
                partner_channel_id=partner_channel_id,
                organization_id=organization_id,
                reason=skip,
            )
            return None

        if list_price is None or net_price is None:
            return None
        margin = list_price - net_price
        partner_share = (
            margin * channel.revenue_share_percent / Decimal("100")
        ).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if partner_share == 0:
            self._log(
                source_type=source_type,
                source_id=source_id,
                customer_user_id=customer_user_id,
                partner_channel_id=partner_channel_id,
                organization_id=organization_id,
                reason="partner_margin.zero_share",
            )
            return None

        team_account = self._team_account(
            channel,
            source_type=source_type,
            source_id=source_id,
            customer_user_id=customer_user_id,
            partner_channel_id=partner_channel_id,
            organization_id=organization_id,
        )
        accrual_id = uuid.uuid4()
        entry = self._credits.credit(
            team_account,
            partner_share,
            reference_type=LedgerReferenceType.PARTNER_MARGIN,
            reference_id=str(accrual_id),
            idempotency_key=f"partner-margin:{source_type}:{source_id}",
        )
        accrual = PartnerMarginAccrual.objects.create(
            id=accrual_id,
            partner_channel=channel,
            customer_user=customer,
            customer_user_id_snapshot=attribution.user_id,
            customer_attribution=attribution,
            source_type=source_type,
            source_id=source_id,
            list_price=list_price,
            net_price=net_price,
            margin=margin,
            revenue_share_percent=channel.revenue_share_percent,
            partner_share=partner_share,
            ledger_entry=entry,
        )
        self._log(
            source_type=source_type,
            source_id=source_id,
            customer_user_id=customer_user_id,
            partner_channel_id=partner_channel_id,
            organization_id=organization_id,
            reason="accrued",
            partner_share=partner_share,
            ledger_entry_id=str(entry.pk),
            accrual_id=str(accrual.pk),
        )
        return accrual

    def _team_account(self, channel: PartnerChannel, **log_fields: object):
        try:
            team_account = channel.organization.account
        except ObjectDoesNotExist:
            team_account = None
        if team_account is None:
            logger.error(
                "partner_margin source_type=%s source_id=%s customer_user_id=%s "
                "partner_channel_id=%s organization_id=%s reason=%s",
                log_fields.get("source_type"),
                log_fields.get("source_id"),
                log_fields.get("customer_user_id"),
                log_fields.get("partner_channel_id"),
                log_fields.get("organization_id"),
                "partner_channel.team_account_missing",
            )
            raise PartnerChannelTeamAccountMissing(
                "Partner channel organization has no team Account"
            )
        return team_account

    @staticmethod
    def _price_skip(
        list_price: Decimal | None,
        net_price: Decimal | None,
        percent: Decimal,
    ) -> str | None:
        if net_price is None:
            return "partner_margin.net_missing"
        if list_price is None:
            return "partner_margin.invalid_margin"
        if net_price < 0:
            return "partner_margin.net_negative"
        if list_price < net_price:
            return "partner_margin.invalid_margin"
        if percent <= 0:
            return "partner_margin.zero_share"
        return None

    @staticmethod
    def _log(
        *,
        source_type: str,
        source_id: str,
        customer_user_id: str | None,
        partner_channel_id: str | None,
        organization_id: str | None,
        reason: str,
        partner_share: Decimal | None = None,
        ledger_entry_id: str | None = None,
        accrual_id: str | None = None,
    ) -> None:
        logger.info(
            "partner_margin source_type=%s source_id=%s customer_user_id=%s "
            "partner_channel_id=%s organization_id=%s reason=%s "
            "partner_share=%s ledger_entry_id=%s accrual_id=%s",
            source_type,
            source_id,
            customer_user_id,
            partner_channel_id,
            organization_id,
            reason,
            partner_share,
            ledger_entry_id,
            accrual_id,
        )


partner_margin_service = PartnerMarginService()
