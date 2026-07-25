"""Print prepaid credit ops metrics."""

from django.core.management.base import BaseCommand

from apps.billing.services.metrics import (
    collect_billing_metrics,
    spend_by_reference_type,
)


class Command(BaseCommand):
    help = (
        "Print billing ops metrics: deposited USDT, deposit count/avg, "
        "spent credits, failed verifies."
    )

    def handle(self, *args, **options) -> None:
        metrics = collect_billing_metrics()
        data = metrics.as_dict()
        for key, value in data.items():
            self.stdout.write(f"{key}={value}")
        for ref, amount in spend_by_reference_type().items():
            self.stdout.write(f"spent[{ref}]={amount}")
