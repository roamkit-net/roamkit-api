"""Assertions against the restored pre-billing dump after migrate to HEAD."""

from __future__ import annotations

import pytest
from django.db import connection

from apps.billing.models import Account
from apps.orders.models import Order


@pytest.mark.django_db
def test_every_user_has_account_and_orders_have_account() -> None:
    assert Account.objects.count() >= 3
    assert Order.objects.count() >= 7
    assert Order.objects.filter(account__isnull=True).count() == 0


@pytest.mark.django_db
def test_order_statuses_survived_migration() -> None:
    statuses = set(Order.objects.values_list("status", flat=True))
    for expected in (
        Order.Status.DRAFT,
        Order.Status.PENDING_PAYMENT,
        Order.Status.PAID,
        Order.Status.FULFILLING,
        Order.Status.FULFILLED,
        Order.Status.FAILED,
        Order.Status.CANCELLED,
    ):
        assert expected in statuses


@pytest.mark.django_db
def test_order_account_backfill_preserves_owner() -> None:
    order = Order.objects.select_related("account__user").get(pk=5)
    assert order.status == Order.Status.FULFILLED
    assert order.account.user.email == "bob@example.com"


@pytest.mark.django_db
def test_orders_table_dropped_user_id() -> None:
    with connection.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'orders_order'
            """)
        columns = {row[0] for row in cur.fetchall()}
    assert "account_id" in columns
    assert "user_id" not in columns
