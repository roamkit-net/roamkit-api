"""Read-only partner channel drift report (ADR 023).

This module does not insert, update, or delete rows and does not call
CreditService.
"""

from __future__ import annotations

from apps.billing.models import AccountKind, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    PartnerChannel,
    PartnerCreditGrant,
    PartnerMarginAccrual,
)


def collect_partner_drift() -> list[str]:
    """Return human-readable drift lines. An empty list is a clean run."""
    issues: list[str] = []
    issues.extend(_accrual_drift())
    issues.extend(_grant_drift())
    issues.extend(_invite_drift())
    return issues


def _accrual_drift() -> list[str]:
    issues: list[str] = []
    seen: set = set()
    accruals = PartnerMarginAccrual.objects.select_related("ledger_entry")
    for accrual in accruals.iterator():
        entry = accrual.ledger_entry
        seen.add(entry.pk)
        if entry.reference_type != LedgerReferenceType.PARTNER_MARGIN:
            issues.append(
                f"accrual {accrual.pk} ledger {entry.pk} is not partner_margin"
            )
        if entry.reference_id != str(accrual.pk):
            issues.append(
                f"accrual {accrual.pk} ledger reference_id is {entry.reference_id}"
            )
        if entry.delta != accrual.partner_share:
            issues.append(
                f"accrual {accrual.pk} partner_share {accrual.partner_share} "
                f"!= ledger delta {entry.delta}"
            )
    orphans = CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exclude(pk__in=seen)
    for entry in orphans.iterator():
        issues.append(f"partner_margin ledger {entry.pk} has no accrual")
    return issues


def _grant_drift() -> list[str]:
    issues: list[str] = []
    seen_out: set = set()
    seen_in: set = set()
    grants = PartnerCreditGrant.objects.select_related(
        "debit_ledger_entry",
        "credit_ledger_entry",
        "credit_ledger_entry__account",
        "partner_channel__organization",
    )
    for grant in grants.iterator():
        debit = grant.debit_ledger_entry
        credit = grant.credit_ledger_entry
        seen_out.add(debit.pk)
        seen_in.add(credit.pk)
        team_id = grant.partner_channel.organization.account_id
        if debit.reference_type != LedgerReferenceType.PARTNER_GRANT_OUT:
            issues.append(f"grant {grant.pk} debit is not partner_grant_out")
        if credit.reference_type != LedgerReferenceType.PARTNER_GRANT_IN:
            issues.append(f"grant {grant.pk} credit is not partner_grant_in")
        if debit.account_id != team_id:
            issues.append(f"grant {grant.pk} debit is not on the team account")
        if credit.account.kind != AccountKind.PERSONAL:
            issues.append(f"grant {grant.pk} credit is not on a personal account")
        elif credit.account.user_id != grant.customer_user_id_snapshot:
            issues.append(
                f"grant {grant.pk} credit account does not match the customer snapshot"
            )
        if abs(debit.delta) != grant.amount or credit.delta != grant.amount:
            issues.append(
                f"grant {grant.pk} amount {grant.amount} != "
                f"debit {debit.delta} credit {credit.delta}"
            )
        if debit.reference_id != str(grant.pk) or credit.reference_id != str(grant.pk):
            issues.append(f"grant {grant.pk} ledger reference_id does not match")
    _orphan_ledgers(issues, LedgerReferenceType.PARTNER_GRANT_OUT, seen_out, "out")
    _orphan_ledgers(issues, LedgerReferenceType.PARTNER_GRANT_IN, seen_in, "in")
    return issues


def _orphan_ledgers(
    issues: list[str], reference_type: str, seen: set, label: str
) -> None:
    orphans = CreditLedgerEntry.objects.filter(reference_type=reference_type).exclude(
        pk__in=seen
    )
    for entry in orphans.iterator():
        issues.append(f"partner_grant_{label} ledger {entry.pk} has no grant")


def _invite_drift() -> list[str]:
    missing = PartnerChannel.objects.filter(invite_link__isnull=True)
    return [f"partner channel {channel.pk} has no invite link" for channel in missing]
