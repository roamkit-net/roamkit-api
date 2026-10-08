"""Delete expired pending partner rows only."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.partner_channel import PendingPartnerAttribution


class Command(BaseCommand):
    help = "Delete PendingPartnerAttribution rows whose expires_at has passed."

    def handle(self, *args, **options) -> None:
        deleted, _ = PendingPartnerAttribution.objects.filter(
            expires_at__lte=timezone.now()
        ).delete()
        self.stdout.write(f"deleted={deleted}")
