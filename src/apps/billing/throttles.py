"""Billing-scoped DRF throttles."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from core import metrics


class BillingVoucherRedeemRateThrottle(SimpleRateThrottle):
    """Limit voucher redeem attempts per authenticated user (ADR 011)."""

    scope = "billing_voucher_redeem"

    def get_rate(self) -> str | None:
        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        try:
            return rates[self.scope]
        except KeyError as exc:
            msg = (
                f"No default throttle rate set for scope '{self.scope}' "
                "in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']."
            )
            raise ImproperlyConfigured(msg) from exc

    def parse_rate(self, rate: str | None) -> tuple[int | None, int | None]:
        """Support ``N/5min`` (DRF built-in only understands s/m/h/d unit letters)."""
        if rate and rate.endswith("/5min"):
            num, _ = rate.split("/", 1)
            return int(num), 300
        return super().parse_rate(rate)

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        if request.method != "POST":
            return None
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": request.user.pk,
        }

    def throttle_failure(self) -> bool:
        metrics.incr(
            "voucher_redeem_failed_total",
            reason="rate_limited",
        )
        return super().throttle_failure()


class PartnerGrantRateThrottle(SimpleRateThrottle):
    """New partner grants per authenticated user and channel.

    Not attached as ``throttle_classes``: an idempotent replay must still
    succeed after the limit. The view calls :meth:`allow_request` only when
    no grant row exists for the key.
    """

    scope = "partner_grant"

    def get_rate(self) -> str | None:
        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        try:
            return rates[self.scope]
        except KeyError as exc:
            msg = (
                f"No default throttle rate set for scope '{self.scope}' "
                "in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']."
            )
            raise ImproperlyConfigured(msg) from exc

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        channel_id = getattr(view, "partner_channel_id", None)
        user = getattr(request, "user", None)
        if channel_id is None or user is None or not user.is_authenticated:
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{user.pk}:{channel_id}",
        }


class PartnerInviteMutationThrottle(PartnerGrantRateThrottle):
    """Invite regenerate/activate/deactivate per user and channel.

    Applied only after the caller is authorized to manage that channel, so an
    inaccessible id does not create a throttle key.
    """

    scope = "partner_invite"
