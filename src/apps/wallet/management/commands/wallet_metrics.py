"""Print Wallet Platform metrics (ADR 017 Cap — Wallet Metrics)."""

from django.core.management.base import BaseCommand

from apps.wallet.services.metrics import collect_wallet_metrics


class Command(BaseCommand):
    help = (
        "Print wallet metrics: allocation, observation, confirmation, convert. "
        "Not a Credits SoT — ledger remains authoritative."
    )

    def handle(self, *args, **options) -> None:
        metrics = collect_wallet_metrics()
        data = metrics.as_dict()
        self.stdout.write(f"wallet_identity_count={data['wallet_identity_count']}")
        self.stdout.write(f"wallet_address_active={data['wallet_address_active']}")
        self.stdout.write(f"wallet_address_retired={data['wallet_address_retired']}")
        self.stdout.write(f"derivation_index_max={data['derivation_index_max']}")
        self.stdout.write(f"pending_confirmation={data['pending_confirmation']}")
        self.stdout.write(
            f"confirmed_awaiting_convert={data['confirmed_awaiting_convert']}"
        )
        self.stdout.write(f"conversion_started={data['conversion_started']}")
        self.stdout.write(f"credited_count={data['credited_count']}")
        self.stdout.write(f"credited_amount_total={data['credited_amount_total']}")
        self.stdout.write(f"rejected_count={data['rejected_count']}")
        self.stdout.write(f"expired_count={data['expired_count']}")
        for status_name, count in sorted(data["observation_counts"].items()):
            self.stdout.write(f"observation[{status_name}]={count}")
