"""Catalog management commands."""

from django.core.management.base import BaseCommand

from apps.catalog.services.package_sync import PackageSyncService
from shared.providers.factory import get_package_provider


class Command(BaseCommand):
    help = "Sync eSIM packages from the configured provider."

    def handle(self, *args, **options):
        service = PackageSyncService(get_package_provider())
        count = service.sync()
        self.stdout.write(
            self.style.SUCCESS(f"Synced {count} package(s) from provider.")
        )
