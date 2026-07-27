"""Voucher redeem service — credit source via CreditService (ADR 011 / 012)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.exceptions import (
    UnsupportedRewardType,
    VoucherError,
    VoucherExpiredError,
    VoucherInvalidError,
    VoucherLimitError,
    VoucherReservedError,
    VoucherRevokedError,
    VouchersDisabledError,
)
from apps.billing.models import (
    Account,
    LedgerReferenceType,
    RedemptionMode,
    RewardType,
    Voucher,
    VoucherBatch,
    VoucherCampaign,
    VoucherRedemption,
    VoucherType,
)
from apps.billing.services.credit import CreditService, credit_service
from apps.billing.voucher_codes import (
    assert_code_available,
    is_reserved_voucher_code,
    normalize_voucher_code,
)
from core import metrics
from shared.events.billing_events import CreditGranted, VoucherRedeemed
from shared.events.event_bus import event_bus

logger = logging.getLogger("roamkit.billing.vouchers")

NowFn = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RedeemResult:
    credited: Decimal
    balance: Decimal
    redemption_id: str
    ledger_entry_id: str
    request_id: str
    replay: bool = False


class VoucherRedeemer(Protocol):
    def redeem(
        self,
        *,
        account: Account,
        code: str,
        request_id: str,
        client_ip: str = "",
        user_agent: str = "",
        now: NowFn | None = None,
    ) -> RedeemResult: ...


class VoucherRedeemService:
    """Redeem SHARED campaign codes or UNIQUE vouchers into prepaid credits.

    Transaction boundary (ADR 011)::

        BEGIN
          → lock Voucher | VoucherCampaign
          → validate
          → INSERT VoucherRedemption
          → CreditService.credit (locks Account → ledger)
          → collect domain events
        COMMIT
          → publish events

    Lock order is always voucher/campaign → Account → ledger (never reversed).
    """

    def __init__(self, credits: CreditService | None = None) -> None:
        self._credits = credits or credit_service

    def redeem(
        self,
        *,
        account: Account,
        code: str,
        request_id: str,
        client_ip: str = "",
        user_agent: str = "",
        now: NowFn | None = None,
    ) -> RedeemResult:
        started = time.perf_counter()
        clock = now or timezone.now
        rid = (request_id or "").strip() or str(uuid4())

        try:
            self._require_enabled()
            result = self._redeem_inner(
                account=account,
                code=code,
                request_id=rid,
                client_ip=client_ip or "",
                user_agent=user_agent or "",
                now=clock,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            metrics.incr("voucher_redeem_success_total")
            logger.info(
                "voucher_redeem_success request_id=%s account_id=%s "
                "redemption_id=%s credited=%s balance=%s replay=%s "
                "duration_ms=%.2f",
                rid,
                account.pk,
                result.redemption_id,
                result.credited,
                result.balance,
                result.replay,
                elapsed_ms,
                extra={
                    "request_id": rid,
                    "account_id": str(account.pk),
                    "redemption_id": result.redemption_id,
                    "metric": "voucher_redeem_duration_seconds",
                    "metric_value": elapsed_ms / 1000.0,
                },
            )
            return result
        except VoucherError as exc:
            self._record_failure(exc, request_id=rid, account_id=str(account.pk))
            raise

    def _redeem_inner(
        self,
        *,
        account: Account,
        code: str,
        request_id: str,
        client_ip: str,
        user_agent: str,
        now: NowFn,
    ) -> RedeemResult:
        normalized = normalize_voucher_code(code)
        if not normalized:
            raise VoucherInvalidError("Voucher code is required")
        if is_reserved_voucher_code(normalized):
            raise VoucherReservedError("Voucher code is reserved")

        events: list[VoucherRedeemed | CreditGranted] = []
        with transaction.atomic():
            voucher = (
                Voucher.objects.select_for_update()
                .filter(
                    code=normalized,
                    redemption_mode=RedemptionMode.UNIQUE,
                )
                .first()
            )
            if voucher is not None:
                result = self._redeem_unique(
                    account=account,
                    voucher=voucher,
                    request_id=request_id,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    now=now,
                    events=events,
                )
            else:
                campaign = (
                    VoucherCampaign.objects.select_for_update()
                    .filter(
                        code=normalized,
                        redemption_mode=RedemptionMode.SHARED,
                    )
                    .first()
                )
                if campaign is None:
                    raise VoucherInvalidError("Invalid voucher code")
                result = self._redeem_shared(
                    account=account,
                    campaign=campaign,
                    request_id=request_id,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    now=now,
                    events=events,
                )

        for event in events:
            event_bus.publish(event)
        return result

    def _redeem_unique(
        self,
        *,
        account: Account,
        voucher: Voucher,
        request_id: str,
        client_ip: str,
        user_agent: str,
        now: NowFn,
        events: list,
    ) -> RedeemResult:
        if voucher.status == Voucher.Status.REDEEMED:
            existing = (
                VoucherRedemption.objects.filter(voucher=voucher, account=account)
                .order_by("redeemed_at")
                .first()
            )
            if existing is not None:
                account.refresh_from_db(fields=["balance"])
                return RedeemResult(
                    credited=existing.amount,
                    balance=account.balance,
                    redemption_id=str(existing.pk),
                    ledger_entry_id=str(existing.ledger_entry_id or ""),
                    request_id=existing.request_id or request_id,
                    replay=True,
                )
            raise VoucherInvalidError("Voucher already redeemed")

        if voucher.status == Voucher.Status.REVOKED:
            raise VoucherRevokedError("Voucher revoked")
        if voucher.status == Voucher.Status.EXPIRED:
            raise VoucherExpiredError("Voucher expired")
        if voucher.status != Voucher.Status.ACTIVE:
            raise VoucherInvalidError("Voucher is not active")

        instant = now()
        if voucher.expires_at is not None and voucher.expires_at <= instant:
            raise VoucherExpiredError("Voucher expired")

        amount = self._credit_amount_for_reward(
            voucher.reward_type, voucher.credit_amount
        )
        return self._grant(
            account=account,
            amount=amount,
            voucher=voucher,
            campaign=None,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            instant=instant,
            events=events,
            mark_voucher_redeemed=voucher,
        )

    def _redeem_shared(
        self,
        *,
        account: Account,
        campaign: VoucherCampaign,
        request_id: str,
        client_ip: str,
        user_agent: str,
        now: NowFn,
        events: list,
    ) -> RedeemResult:
        if campaign.status == VoucherCampaign.Status.REVOKED:
            raise VoucherRevokedError("Campaign revoked")
        if campaign.status == VoucherCampaign.Status.EXPIRED:
            raise VoucherExpiredError("Campaign expired")
        if campaign.status != VoucherCampaign.Status.ACTIVE:
            raise VoucherInvalidError("Campaign is not active")

        instant = now()
        if campaign.starts_at is not None and campaign.starts_at > instant:
            raise VoucherInvalidError("Campaign is not active yet")
        if campaign.expires_at is not None and campaign.expires_at <= instant:
            raise VoucherExpiredError("Campaign expired")

        prior = (
            VoucherRedemption.objects.filter(campaign=campaign, account=account)
            .order_by("redeemed_at")
            .first()
        )
        if campaign.max_redemptions_per_account is not None:
            used = VoucherRedemption.objects.filter(
                campaign=campaign, account=account
            ).count()
            if used >= campaign.max_redemptions_per_account:
                if (
                    campaign.max_redemptions_per_account == 1
                    and prior is not None
                ):
                    account.refresh_from_db(fields=["balance"])
                    return RedeemResult(
                        credited=prior.amount,
                        balance=account.balance,
                        redemption_id=str(prior.pk),
                        ledger_entry_id=str(prior.ledger_entry_id or ""),
                        request_id=prior.request_id or request_id,
                        replay=True,
                    )
                raise VoucherLimitError("Redemption limit reached for this account")

        if campaign.max_redemptions_total is not None:
            total = VoucherRedemption.objects.filter(campaign=campaign).count()
            if total >= campaign.max_redemptions_total:
                raise VoucherLimitError("Campaign redemption limit reached")

        amount = self._credit_amount_for_reward(
            campaign.reward_type, campaign.credit_amount
        )
        return self._grant(
            account=account,
            amount=amount,
            voucher=None,
            campaign=campaign,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            instant=instant,
            events=events,
            mark_voucher_redeemed=None,
        )

    def _grant(
        self,
        *,
        account: Account,
        amount: Decimal,
        voucher: Voucher | None,
        campaign: VoucherCampaign | None,
        request_id: str,
        client_ip: str,
        user_agent: str,
        instant: datetime,
        events: list,
        mark_voucher_redeemed: Voucher | None,
    ) -> RedeemResult:
        redemption = VoucherRedemption(
            account=account,
            voucher=voucher,
            campaign=campaign,
            amount=amount,
            request_id=request_id,
            redeemed_at=instant,
            redeemed_ip=client_ip[:64],
            redeemed_user_agent=user_agent[:512],
        )
        redemption.save()

        entry = self._credits.credit(
            account,
            amount,
            reference_type=LedgerReferenceType.VOUCHER,
            reference_id=str(redemption.pk),
            idempotency_key=f"voucher_redeem:{redemption.pk}",
        )
        redemption.ledger_entry_id = entry.pk
        redemption.save(update_fields=["ledger_entry_id"])

        if mark_voucher_redeemed is not None:
            mark_voucher_redeemed.status = Voucher.Status.REDEEMED
            mark_voucher_redeemed.save(update_fields=["status", "updated_at"])

        events.append(
            VoucherRedeemed(
                voucher_id=str(voucher.pk) if voucher else None,
                campaign_id=str(campaign.pk) if campaign else None,
                redemption_id=str(redemption.pk),
                account_id=str(account.pk),
                amount=amount,
                balance_after=entry.balance_after,
                ledger_entry_id=str(entry.pk),
                request_id=request_id,
                redeemed_at=instant,
            )
        )
        events.append(
            CreditGranted(
                account_id=str(account.pk),
                amount=amount,
                balance_after=entry.balance_after,
                reference_type=LedgerReferenceType.VOUCHER,
                reference_id=str(redemption.pk),
                ledger_entry_id=str(entry.pk),
                created_at=entry.created_at,
            )
        )
        return RedeemResult(
            credited=amount,
            balance=entry.balance_after,
            redemption_id=str(redemption.pk),
            ledger_entry_id=str(entry.pk),
            request_id=request_id,
        )

    @staticmethod
    def _credit_amount_for_reward(reward_type: str, credit_amount: Decimal) -> Decimal:
        if reward_type == RewardType.FIXED_CREDIT:
            return credit_amount
        raise UnsupportedRewardType(f"Unsupported reward type: {reward_type}")

    @staticmethod
    def _require_enabled() -> None:
        if not settings.BILLING_ENABLED or not settings.VOUCHERS_ENABLED:
            raise VouchersDisabledError("Vouchers are disabled")

    @staticmethod
    def _record_failure(
        exc: VoucherError,
        *,
        request_id: str,
        account_id: str,
    ) -> None:
        fail_reason = exc.code
        metrics.incr("voucher_redeem_failed_total", reason=fail_reason)
        if fail_reason in {"voucher_invalid", "voucher_reserved"}:
            metrics.incr("voucher_redeem_invalid_total", reason=fail_reason)
        elif fail_reason == "voucher_expired":
            metrics.incr("voucher_redeem_expired_total")
        elif fail_reason == "voucher_revoked":
            metrics.incr("voucher_redeem_revoked_total")
        logger.info(
            "voucher_redeem_failed request_id=%s account_id=%s reason=%s detail=%s",
            request_id,
            account_id,
            fail_reason,
            exc.detail,
            extra={
                "request_id": request_id,
                "account_id": account_id,
                "reason": fail_reason,
            },
        )


def issue_shared_campaign(
    *,
    code: str,
    credit_amount: Decimal,
    max_redemptions_total: int | None = None,
    max_redemptions_per_account: int | None = 1,
    status: str = VoucherCampaign.Status.ACTIVE,
    voucher_type: str = VoucherType.PROMO,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> VoucherCampaign:
    """Create an ACTIVE shared campaign (tests / future admin)."""
    normalized = assert_code_available(code)
    return VoucherCampaign.objects.create(
        code=normalized,
        redemption_mode=RedemptionMode.SHARED,
        voucher_type=voucher_type,
        reward_type=RewardType.FIXED_CREDIT,
        credit_amount=credit_amount,
        max_redemptions_total=max_redemptions_total,
        max_redemptions_per_account=max_redemptions_per_account,
        starts_at=starts_at,
        expires_at=expires_at,
        status=status,
    )


def issue_unique_voucher(
    *,
    code: str,
    credit_amount: Decimal,
    status: str = Voucher.Status.ACTIVE,
    voucher_type: str = VoucherType.GIFT,
    expires_at: datetime | None = None,
    campaign: VoucherCampaign | None = None,
    batch: VoucherBatch | None = None,
) -> Voucher:
    """Create an ACTIVE unique voucher (tests / future admin)."""
    normalized = assert_code_available(code)
    return Voucher.objects.create(
        code=normalized,
        redemption_mode=RedemptionMode.UNIQUE,
        voucher_type=voucher_type,
        reward_type=RewardType.FIXED_CREDIT,
        credit_amount=credit_amount,
        status=status,
        expires_at=expires_at,
        campaign=campaign,
        batch=batch,
    )


voucher_redeem_service = VoucherRedeemService()
