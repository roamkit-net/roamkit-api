"""Read-only partner integrity check. Does not repair rows."""

import json
import sys

from django.core.management.base import BaseCommand

from apps.billing.services.partner_integrity import collect_partner_integrity


class Command(BaseCommand):
    help = (
        "Report partner ownership, attribution, grant, margin, and settlement "
        "integrity. Does not repair, rotate tokens, or change balances."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="text is the default. json prints one object with an issues list.",
        )

    def handle(self, *args, **options) -> None:
        issues = collect_partner_integrity()
        if options["format"] == "json":
            payload = {
                "status": "clean" if not issues else "failed",
                "count": len(issues),
                "issues": [issue.as_dict() for issue in issues],
            }
            self.stdout.write(json.dumps(payload, sort_keys=True))
        elif not issues:
            self.stdout.write("partner_integrity_check=clean")
        else:
            for issue in issues:
                self.stdout.write(issue.render())
        if issues:
            self.stderr.write(f"partner_integrity_check violations={len(issues)}")
            sys.exit(1)
