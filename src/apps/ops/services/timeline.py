"""Build generic OpsEvent DTOs from domain rows (no event-bus dependency)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model

from apps.billing.models import (
    CreditLedgerEntry,
    DepositRequest,
    LedgerReferenceType,
    VoucherRedemption,
)
from apps.esims.models import EsimLifecycleEvent, Topup
from apps.orders.models import Order

User = get_user_model()

OPS_EVENT_SCHEMA_VERSION = 1

EventGroup = str  # account | billing | order | esim | wallet | voucher
Severity = str  # info | warning | error


@dataclass(frozen=True, slots=True)
class OpsEvent:
    """Stable timeline / activity feed item (schema_version bumped only on breaks)."""

    schema_version: int
    type: str
    timestamp: datetime
    title: str
    subtitle: str
    reference_id: str
    severity: Severity
    event_group: EventGroup
    icon: str
    user_id: int | None = None
    user_email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        ts = data["timestamp"]
        if isinstance(ts, datetime):
            data["timestamp"] = ts.isoformat().replace("+00:00", "Z")
        return data


def _sid(value: UUID | int | str | None) -> str:
    if value is None:
        return ""
    return str(value)


def _money(amount: Decimal | None) -> str:
    if amount is None:
        return ""
    return f"{amount:.6f}".rstrip("0").rstrip(".") or "0"


def _severity_for_status(status: str) -> Severity:
    if status in {"failed", "cancelled", "exhausted", "expired"}:
        return "error"
    if status in {
        "pending",
        "pending_payment",
        "paid",
        "fulfilling",
        "draft",
        "purchased",
        "installation_started",
        "unknown",
    }:
        return "warning"
    return "info"


def _event(
    *,
    type_: str,
    timestamp: datetime,
    title: str,
    subtitle: str = "",
    reference_id: str = "",
    severity: Severity = "info",
    event_group: EventGroup,
    user_id: int | None = None,
    user_email: str | None = None,
) -> OpsEvent:
    return OpsEvent(
        schema_version=OPS_EVENT_SCHEMA_VERSION,
        type=type_,
        timestamp=timestamp,
        title=title,
        subtitle=subtitle,
        reference_id=reference_id,
        severity=severity,
        event_group=event_group,
        icon=event_group,
        user_id=user_id,
        user_email=user_email,
    )


def _user_fields(user: Any | None) -> tuple[int | None, str | None]:
    if user is None:
        return None, None
    return getattr(user, "pk", None), getattr(user, "email", None)


def event_from_ledger(entry: CreditLedgerEntry) -> OpsEvent:
    account = entry.account
    user = getattr(account, "user", None)
    uid, email = _user_fields(user)
    ref = entry.reference_type
    titles = {
        LedgerReferenceType.DEPOSIT: "Deposit credited",
        LedgerReferenceType.ORDER: "Order spend",
        LedgerReferenceType.TOPUP: "Top-up spend",
        LedgerReferenceType.SUBSCRIPTION: "Subscription charge",
        LedgerReferenceType.REFUND: "Refund",
        LedgerReferenceType.ADMIN_ADJUSTMENT: "Admin adjustment",
        LedgerReferenceType.VOUCHER: "Voucher credit",
    }
    title = titles.get(ref, f"Ledger ({ref})")
    delta = entry.delta
    sign = "+" if delta >= 0 else ""
    subtitle = f"{sign}{_money(delta)} · balance {_money(entry.balance_after)}"
    group = "voucher" if ref == LedgerReferenceType.VOUCHER else "billing"
    severity: Severity = "info"
    if ref == LedgerReferenceType.REFUND:
        severity = "warning"
    elif delta < 0:
        severity = "info"
    return _event(
        type_=f"ledger.{ref}",
        timestamp=entry.created_at,
        title=title,
        subtitle=subtitle,
        reference_id=_sid(entry.pk),
        severity=severity,
        event_group=group,
        user_id=uid,
        user_email=email,
    )


def event_from_order(order: Order) -> OpsEvent:
    user = getattr(order.account, "user", None)
    uid, email = _user_fields(user)
    title = f"Order {order.status.replace('_', ' ')}"
    parts = [order.package_title or "Package"]
    if order.retail_price_usd is not None:
        parts.append(f"${order.retail_price_usd}")
    return _event(
        type_=f"order.{order.status}",
        timestamp=order.updated_at or order.created_at,
        title=title,
        subtitle=" · ".join(parts),
        reference_id=_sid(order.pk),
        severity=_severity_for_status(order.status),
        event_group="order",
        user_id=uid,
        user_email=email,
    )


def event_from_deposit(deposit: DepositRequest) -> OpsEvent:
    user = getattr(deposit.account, "user", None)
    uid, email = _user_fields(user)
    title = f"Deposit {deposit.status}"
    amount = deposit.amount_credited or deposit.amount_requested
    subtitle = f"{_money(amount)} · {deposit.payment_method}"
    return _event(
        type_=f"deposit.{deposit.status}",
        timestamp=deposit.updated_at or deposit.created_at,
        title=title,
        subtitle=subtitle,
        reference_id=_sid(deposit.pk),
        severity=_severity_for_status(deposit.status),
        event_group="billing",
        user_id=uid,
        user_email=email,
    )


def event_from_topup(topup: Topup) -> OpsEvent:
    user = getattr(topup.account, "user", None)
    uid, email = _user_fields(user)
    iccid = getattr(topup.esim, "iccid", "") if topup.esim_id else ""
    subtitle = f"{_money(topup.amount)}"
    if iccid:
        subtitle = f"{subtitle} · {iccid}"
    return _event(
        type_=f"topup.{topup.status}",
        timestamp=topup.updated_at or topup.created_at,
        title=f"Top-up {topup.status}",
        subtitle=subtitle,
        reference_id=_sid(topup.pk),
        severity=_severity_for_status(topup.status),
        event_group="billing",
        user_id=uid,
        user_email=email,
    )


def event_from_lifecycle(ev: EsimLifecycleEvent) -> OpsEvent:
    uid, email = _user_fields(ev.user)
    iccid = getattr(ev.esim, "iccid", "") if ev.esim_id else ""
    return _event(
        type_=f"esim.{ev.event_type}",
        timestamp=ev.created_at,
        title=ev.event_type.replace(".", " ").replace("_", " "),
        subtitle=iccid,
        reference_id=_sid(ev.pk),
        severity="info",
        event_group="esim",
        user_id=uid,
        user_email=email,
    )


def event_from_redemption(redemption: VoucherRedemption) -> OpsEvent:
    user = getattr(redemption.account, "user", None)
    uid, email = _user_fields(user)
    code = ""
    if redemption.voucher_id and redemption.voucher:
        code = redemption.voucher.code
    subtitle = _money(redemption.amount)
    if code:
        subtitle = f"{subtitle} · {code}"
    return _event(
        type_="voucher.redeemed",
        timestamp=redemption.redeemed_at,
        title="Voucher redeemed",
        subtitle=subtitle,
        reference_id=_sid(redemption.pk),
        severity="info",
        event_group="voucher",
        user_id=uid,
        user_email=email,
    )


def event_from_user_created(user: User) -> OpsEvent:
    return _event(
        type_="user.created",
        timestamp=user.created_at,
        title="Account created",
        subtitle=user.email,
        reference_id=_sid(user.pk),
        severity="info",
        event_group="account",
        user_id=user.pk,
        user_email=user.email,
    )


def _merge_events(events: list[OpsEvent], *, limit: int) -> list[dict[str, Any]]:
    events.sort(key=lambda e: e.timestamp, reverse=True)
    return [e.to_dict() for e in events[:limit]]


def build_global_activity(*, limit: int = 50) -> list[dict[str, Any]]:
    """Last N ops events across all users (bounded queries, then merge)."""
    fetch = max(limit, 50)
    events: list[OpsEvent] = []

    ledger_qs = CreditLedgerEntry.objects.select_related("account__user").order_by(
        "-created_at"
    )[:fetch]
    events.extend(event_from_ledger(e) for e in ledger_qs)

    order_qs = Order.objects.select_related("account__user").order_by("-updated_at")[
        :fetch
    ]
    events.extend(event_from_order(o) for o in order_qs)

    deposit_qs = DepositRequest.objects.select_related("account__user").order_by(
        "-updated_at"
    )[:fetch]
    events.extend(event_from_deposit(d) for d in deposit_qs)

    topup_qs = Topup.objects.select_related("account__user", "esim").order_by(
        "-updated_at"
    )[:fetch]
    events.extend(event_from_topup(t) for t in topup_qs)

    life_qs = EsimLifecycleEvent.objects.select_related("user", "esim").order_by(
        "-created_at"
    )[:fetch]
    events.extend(event_from_lifecycle(ev) for ev in life_qs)

    redeem_qs = VoucherRedemption.objects.select_related(
        "account__user", "voucher"
    ).order_by("-redeemed_at")[:fetch]
    events.extend(event_from_redemption(r) for r in redeem_qs)

    return _merge_events(events, limit=limit)


def build_user_timeline(user: User, *, limit: int = 100) -> list[dict[str, Any]]:
    """Member-scoped timeline using the same OpsEvent DTO."""
    account = getattr(user, "billing_account", None)
    events: list[OpsEvent] = [event_from_user_created(user)]
    fetch = max(limit, 100)

    if account is not None:
        # Attach user for event mappers without extra queries.
        account.user = user

        ledger_qs = (
            CreditLedgerEntry.objects.filter(account=account)
            .select_related("account")
            .order_by("-created_at")[:fetch]
        )
        for entry in ledger_qs:
            entry.account = account
            events.append(event_from_ledger(entry))

        order_qs = Order.objects.filter(account=account).order_by("-updated_at")[:fetch]
        for order in order_qs:
            order.account = account
            events.append(event_from_order(order))

        deposit_qs = DepositRequest.objects.filter(account=account).order_by(
            "-updated_at"
        )[:fetch]
        for deposit in deposit_qs:
            deposit.account = account
            events.append(event_from_deposit(deposit))

        topup_qs = (
            Topup.objects.filter(account=account)
            .select_related("esim")
            .order_by("-updated_at")[:fetch]
        )
        for topup in topup_qs:
            topup.account = account
            events.append(event_from_topup(topup))

        redeem_qs = (
            VoucherRedemption.objects.filter(account=account)
            .select_related("voucher")
            .order_by("-redeemed_at")[:fetch]
        )
        for redemption in redeem_qs:
            redemption.account = account
            events.append(event_from_redemption(redemption))

    life_qs = (
        EsimLifecycleEvent.objects.filter(user=user)
        .select_related("esim", "user")
        .order_by("-created_at")[:fetch]
    )
    events.extend(event_from_lifecycle(ev) for ev in life_qs)

    return _merge_events(events, limit=limit)
