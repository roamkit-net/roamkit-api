"""eSIM provider protocols and domain DTOs."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PackageFilters:
    """Filters passed to package providers when listing catalog data."""

    country_code: str | None = None


@dataclass(frozen=True)
class PackageDTO:
    """Normalized package from an external provider."""

    external_id: str
    title: str
    operator_title: str
    operator_id: str
    country_code: str
    data_allowance: str
    validity_days: int
    price_usd: Decimal
    net_price_usd: Decimal | None
    is_unlimited: bool
    plan_type: str
    voice_minutes: int | None = None
    text_sms: int | None = None
    location_slug: str = ""
    location_title: str = ""
    location_image_url: str = ""
    coverage_type: str = "local"
    covered_country_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderedSimDTO:
    """A single eSIM provisioned as part of an order."""

    iccid: str
    lpa: str
    matching_id: str
    qrcode: str
    qrcode_url: str
    direct_apple_installation_url: str


@dataclass(frozen=True)
class OrderResult:
    """Normalized result of placing an eSIM order with a provider."""

    external_order_id: str
    code: str
    package_id: str
    customer_ref: str
    currency: str
    price_usd: Decimal
    manual_installation: str
    qrcode_installation: str
    installation_guide_url: str
    sims: list[OrderedSimDTO]


@dataclass(frozen=True)
class UsageDTO:
    """Normalized usage snapshot for an eSIM."""

    remaining_mb: int
    total_mb: int
    expired_at: str | None
    is_unlimited: bool | None
    status: str
    remaining_voice: int
    remaining_text: int
    total_voice: int
    total_text: int


@dataclass(frozen=True)
class TopupPackage:
    """A top-up package available for a specific eSIM."""

    external_id: str
    title: str
    data_allowance: str
    validity_days: int
    price_usd: Decimal
    net_price_usd: Decimal | None
    is_unlimited: bool
    plan_type: str


@dataclass(frozen=True)
class TopupResult:
    """Normalized result of submitting a top-up order."""

    external_order_id: str
    code: str
    package_id: str
    iccid: str
    currency: str
    price_usd: Decimal
    customer_ref: str


class PackageProvider(Protocol):
    """Lists purchasable eSIM packages from an external wholesaler."""

    def list_packages(self, filters: PackageFilters) -> list[PackageDTO]: ...


class OrderProvider(Protocol):
    """Places eSIM orders with an external wholesaler."""

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult: ...


class TopupProvider(Protocol):
    """Lists top-ups, submits top-up orders, and fetches usage."""

    def list_topups(self, iccid: str) -> list[TopupPackage]: ...

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult: ...

    def get_usage(self, iccid: str) -> UsageDTO: ...
