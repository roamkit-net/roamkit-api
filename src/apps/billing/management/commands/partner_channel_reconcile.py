"""Report partner channel drift. Read only."""

import sys

from django.core.management.base import BaseCommand

from apps.billing.services.partner_reconcile import collect_partner_drift


class Command(BaseCommand):
    help = "Report partner accrual, grant, and invite-link drift. Does not repair."

    def handle(self, *args, **options) -> None:
        issues = collect_partner_drift()
        if not issues:
            self.stdout.write("partner_channel_reconcile=clean")
            return
        for issue in issues:
            self.stdout.write(issue)
        self.stderr.write(f"partner_channel_reconcile drift={len(issues)}")
        sys.exit(1)
