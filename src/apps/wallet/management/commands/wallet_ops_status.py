"""Print Wallet Platform ops status (ADR 017 Failure Domains drills)."""

from django.core.management.base import BaseCommand

from apps.wallet.services.ops import collect_wallet_ops_status


class Command(BaseCommand):
    help = (
        "Print wallet ops status: observation counts, convert backlog, "
        "address count, seed configured flag. Not a Credits SoT."
    )

    def handle(self, *args, **options) -> None:
        status = collect_wallet_ops_status()
        data = status.as_dict()
        self.stdout.write(f"wallet_address_count={data['wallet_address_count']}")
        self.stdout.write(f"seed_configured={data['seed_configured']}")
        self.stdout.write(
            f"confirmed_awaiting_convert={data['confirmed_awaiting_convert']}"
        )
        self.stdout.write(f"conversion_started={data['conversion_started']}")
        self.stdout.write(f"pending_confirmation={data['pending_confirmation']}")
        for status_name, count in sorted(data["observation_counts"].items()):
            self.stdout.write(f"observation[{status_name}]={count}")
