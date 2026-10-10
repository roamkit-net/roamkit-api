"""Read-only partner integrity report (ADR 024).

Does not insert, update, delete, rotate tokens, or call CreditService mutators.
Balance comparison uses ``CreditService.ledger_sum`` and does not rebuild.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from django.db.models import Exists, F, OuterRef

from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
    PartnerInviteLink,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_reconcile import collect_partner_drift
from apps.billing.services.partner_settlement import (
    PartnerChannelOwnershipInvalid,
    PartnerSettlementAccountMissing,
    resolve_partner_settlement_account,
)
from apps.organizations.models import Membership, MembershipStatus

_MONEY = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class PartnerIntegrityIssue:
    code: str
    channel_id: str | None = None
    kind: str | None = None
    owner: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def render(self) -> str:
        return (
            f"code={self.code} channel_id={self.channel_id or '-'} "
            f"kind={self.kind or '-'} owner={self.owner or '-'} "
            f"detail={self.detail or '-'}"
        )


def collect_partner_integrity() -> list[PartnerIntegrityIssue]:
    """Return every integrity problem. An empty list is a clean database."""
    issues: list[PartnerIntegrityIssue] = [
        PartnerIntegrityIssue(code="partner_drift", detail=line)
        for line in collect_partner_drift()
    ]
    issues.extend(_channel_settlement())
    issues.extend(_attribution_conflicts())
    issues.extend(_grant_shape())
    issues.extend(_invite_shape())
    return issues


def _owner_label(channel: PartnerChannel) -> str:
    if channel.owner_user_id is not None:
        return f"user:{channel.owner_user_id}"
    if channel.organization_id is not None:
        return f"org:{channel.organization_id}"
    return "missing"


def _issue(
    code: str,
    channel: PartnerChannel | None = None,
    *,
    detail: str = "",
) -> PartnerIntegrityIssue:
    if channel is None:
        return PartnerIntegrityIssue(code=code, detail=detail)
    return PartnerIntegrityIssue(
        code=code,
        channel_id=str(channel.pk),
        kind=channel.kind,
        owner=_owner_label(channel),
        detail=detail,
    )


def _channel_settlement() -> list[PartnerIntegrityIssue]:
    issues: list[PartnerIntegrityIssue] = []
    channels = PartnerChannel.objects.select_related(
        "owner_user",
        "organization",
    ).order_by("pk")
    for channel in channels.iterator():
        try:
            account = resolve_partner_settlement_account(channel)
        except PartnerSettlementAccountMissing:
            issues.append(_issue("settlement_account_missing", channel))
            continue
        except PartnerChannelOwnershipInvalid:
            issues.append(_issue("partner_channel_ownership_invalid", channel))
            continue
        expected = credit_service.ledger_sum(account)
        if account.balance != expected:
            issues.append(_issue("settlement_balance_drift", channel))
    return issues


def _attribution_conflicts() -> list[PartnerIntegrityIssue]:
    """One issue per illegal current attribution.

    A team member who is also that channel's current customer is one pair.
    The membership side is the same pair, so it is not reported again.
    """
    issues: list[PartnerIntegrityIssue] = []
    individual = CustomerAttribution.objects.select_related("partner_channel").filter(
        partner_channel__kind=PartnerChannel.Kind.INDIVIDUAL,
        user_id=F("partner_channel__owner_user_id"),
    )
    for row in individual.iterator():
        issues.append(_issue("individual_self_attribution", row.partner_channel))
    active_member = Membership.objects.filter(
        organization_id=OuterRef("partner_channel__organization_id"),
        user_id=OuterRef("user_id"),
        status=MembershipStatus.ACTIVE,
    )
    team = (
        CustomerAttribution.objects.select_related("partner_channel")
        .filter(
            partner_channel__kind=PartnerChannel.Kind.TEAM,
            partner_channel__organization_id__isnull=False,
        )
        .filter(Exists(active_member))
    )
    for row in team.iterator():
        issues.append(
            _issue(
                "team_self_attribution",
                row.partner_channel,
                detail=f"user:{row.user_id}",
            )
        )
    return issues


def _grant_shape() -> list[PartnerIntegrityIssue]:
    issues: list[PartnerIntegrityIssue] = []
    grants = PartnerCreditGrant.objects.select_related(
        "partner_channel",
        "debit_ledger_entry",
        "credit_ledger_entry",
    )
    for grant in grants.iterator():
        channel = grant.partner_channel
        debit = grant.debit_ledger_entry
        credit = grant.credit_ledger_entry
        if debit.account_id == credit.account_id:
            issues.append(_issue("grant_same_account", channel))
        if grant.amount != grant.amount.quantize(_MONEY):
            issues.append(_issue("grant_amount_precision", channel))
    return issues


def _invite_shape() -> list[PartnerIntegrityIssue]:
    issues: list[PartnerIntegrityIssue] = []
    links = PartnerInviteLink.objects.select_related("partner_channel")
    for link in links.iterator():
        if not link.token:
            issues.append(_issue("invite_token_missing", link.partner_channel))
    return issues
