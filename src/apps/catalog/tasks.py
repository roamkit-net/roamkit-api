"""Catalog Celery tasks."""

from celery import shared_task

from apps.catalog.services.package_sync import PackageSyncService
from shared.providers.factory import get_package_provider


@shared_task(name="catalog.sync_airalo_packages")
def sync_airalo_packages() -> int:
    """Sync the package catalog from Airalo."""
    service = PackageSyncService(get_package_provider())
    return service.sync()
