"""Provider factory for dependency injection."""

from django.conf import settings
from django.utils.module_loading import import_string

from shared.providers.esim import OrderProvider, PackageProvider, TopupProvider


def get_package_provider() -> PackageProvider:
    """Return the configured package provider implementation."""
    provider_class = import_string(settings.PACKAGE_PROVIDER)
    return provider_class()


def get_order_provider() -> OrderProvider:
    """Return the configured order provider implementation."""
    provider_class = import_string(settings.ORDER_PROVIDER)
    return provider_class()


def get_topup_provider() -> TopupProvider:
    """Return the configured top-up provider implementation."""
    provider_class = import_string(settings.TOPUP_PROVIDER)
    return provider_class()
