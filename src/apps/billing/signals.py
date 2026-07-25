"""Ensure every User gets a billing Account."""

from __future__ import annotations

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.billing.services import ensure_billing_account


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_billing_account_for_user(sender, instance, created: bool, **kwargs) -> None:
    if created:
        ensure_billing_account(instance)
