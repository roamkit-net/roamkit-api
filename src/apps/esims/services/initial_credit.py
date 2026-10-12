"""Sequence-0 top-up row for the original eSIM purchase.

This row is a record of the first credit. It does not debit the ledger.
"""

from __future__ import annotations

from decimal import Decimal

from apps.esims.models import Esim, Topup

INITIAL_CREDIT_KEY_PREFIX = "initial-credit:"


def initial_credit_key(esim_id: int) -> str:
    return f"{INITIAL_CREDIT_KEY_PREFIX}{esim_id}"


def ensure_initial_credit_topup(esim: Esim) -> Topup:
    """Create the sequence-0 row from the purchase snapshot when it is missing."""
    key = initial_credit_key(esim.pk)
    existing = Topup.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing

    order = esim.order
    package = order.package
    external_id = (order.package_external_id or "").strip() or package.external_id
    amount = order.retail_price_usd
    if amount is None or amount <= 0:
        amount = package.price_usd
    if amount is None or amount <= Decimal("0"):
        raise ValueError(
            f"Cannot record initial credit for esim_id={esim.pk}: "
            "amount is not positive"
        )

    topup = Topup.objects.create(
        account_id=esim.account_id,
        esim=esim,
        package_external_id=external_id,
        amount=amount,
        status=Topup.Status.FULFILLED,
        external_order_id=(order.external_order_id or "")[:64],
        idempotency_key=key,
        sequence=0,
        list_price_usd=order.list_price_usd,
    )
    Topup.objects.filter(pk=topup.pk).update(created_at=order.created_at)
    topup.created_at = order.created_at
    return topup
