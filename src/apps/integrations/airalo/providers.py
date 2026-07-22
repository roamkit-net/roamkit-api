"""Airalo provider implementations."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from apps.integrations.airalo.client import AiraloClient
from shared.providers.esim import (
    OrderedSimDTO,
    OrderResult,
    PackageDTO,
    PackageFilters,
    TopupPackage,
    TopupResult,
    UsageDTO,
)

COVERAGE_LOCAL = "local"
COVERAGE_REGIONAL = "regional"
COVERAGE_GLOBAL = "global"


class AiraloPackageProvider:
    """Maps Airalo Partner API packages to domain DTOs."""

    def __init__(self, client: AiraloClient | None = None) -> None:
        self.client = client or AiraloClient()

    def list_packages(self, filters: PackageFilters) -> list[PackageDTO]:
        items = self.client.list_packages(country_code=filters.country_code)
        packages: list[PackageDTO] = []

        for item in items:
            # GET /v2/packages returns countries with nested operators.
            if "operators" in item:
                packages.extend(self._map_location_item(item))
            else:
                packages.extend(self._map_operator(item))

        return packages

    def _map_location_item(self, item: dict[str, Any]) -> list[PackageDTO]:
        location_slug = str(item.get("slug") or "").strip()
        location_title = str(item.get("title") or "").strip()
        country_code = str(item.get("country_code") or "").upper()
        country_code = AiraloPackageProvider._normalize_iso2(country_code)
        image_url = self._extract_image_url(item.get("image"))

        mapped: list[PackageDTO] = []
        for operator in item.get("operators") or []:
            if not isinstance(operator, dict):
                continue
            mapped.extend(
                self._map_operator(
                    operator,
                    country_code_override=country_code,
                    location_slug=location_slug,
                    location_title=location_title,
                    location_image_url=image_url,
                )
            )
        return mapped

    def _map_operator(
        self,
        operator: dict[str, Any],
        *,
        country_code_override: str | None = None,
        location_slug: str = "",
        location_title: str = "",
        location_image_url: str = "",
    ) -> list[PackageDTO]:
        operator_id = str(operator.get("id", ""))
        operator_title = str(operator.get("title", ""))
        plan_type = str(operator.get("plan_type", "data"))
        countries = operator.get("countries") or []
        if country_code_override is not None:
            country_code = self._normalize_iso2(country_code_override)
        else:
            country_code = self._normalize_iso2(self._primary_country_code(countries))
        covered_codes = self._covered_country_codes(countries)
        coverage_type = self._resolve_coverage_type(
            operator_type=str(operator.get("type") or ""),
            location_slug=location_slug,
            country_code=country_code,
        )
        if coverage_type != COVERAGE_LOCAL:
            country_code = ""

        resolved_slug = location_slug or self._fallback_slug(
            coverage_type=coverage_type,
            country_code=country_code,
            operator_title=operator_title,
        )
        resolved_title = location_title or operator_title or resolved_slug

        mapped: list[PackageDTO] = []
        for package in operator.get("packages", []):
            dto = self._map_package(
                package,
                operator_id=operator_id,
                operator_title=operator_title,
                plan_type=plan_type,
                country_code=country_code,
                location_slug=resolved_slug,
                location_title=resolved_title,
                location_image_url=location_image_url,
                coverage_type=coverage_type,
                covered_country_codes=covered_codes,
            )
            if dto is not None:
                mapped.append(dto)
        return mapped

    def _map_package(
        self,
        package: dict[str, Any],
        *,
        operator_id: str,
        operator_title: str,
        plan_type: str,
        country_code: str,
        location_slug: str,
        location_title: str,
        location_image_url: str,
        coverage_type: str,
        covered_country_codes: tuple[str, ...],
    ) -> PackageDTO | None:
        external_id = str(package.get("id", "")).strip()
        if not external_id:
            return None

        price_usd = self._extract_usd_price(package)
        net_price_usd = self._extract_usd_net_price(package)
        title = str(package.get("title", ""))
        voice_minutes = self._optional_positive_int(package.get("voice"))
        text_sms = self._optional_positive_int(package.get("text"))
        if voice_minutes is None and text_sms is None:
            parsed_voice, parsed_text = self._parse_voice_text_from_title(title)
            voice_minutes = parsed_voice
            text_sms = parsed_text

        return PackageDTO(
            external_id=external_id,
            title=title,
            operator_title=operator_title,
            operator_id=operator_id,
            country_code=country_code,
            data_allowance=self._format_data_allowance(package),
            validity_days=int(package.get("day") or 0),
            price_usd=price_usd,
            net_price_usd=net_price_usd,
            is_unlimited=bool(package.get("is_unlimited", False)),
            plan_type=plan_type,
            voice_minutes=voice_minutes,
            text_sms=text_sms,
            location_slug=location_slug,
            location_title=location_title,
            location_image_url=location_image_url,
            coverage_type=coverage_type,
            covered_country_codes=covered_country_codes,
        )

    @staticmethod
    def _resolve_coverage_type(
        *,
        operator_type: str,
        location_slug: str,
        country_code: str,
    ) -> str:
        slug = location_slug.strip().lower()
        if slug in {"world", "global"}:
            return COVERAGE_GLOBAL

        normalized_type = operator_type.strip().lower()
        if normalized_type == "local":
            return COVERAGE_LOCAL

        # Airalo marks both regional and worldwide as type=global; slug distinguishes.
        if normalized_type in {"global", "worldwide", "regional", "region"}:
            return COVERAGE_REGIONAL

        # Regional/global packages often have an empty country_code.
        if not country_code:
            return COVERAGE_REGIONAL
        return COVERAGE_LOCAL

    @staticmethod
    def _fallback_slug(
        *,
        coverage_type: str,
        country_code: str,
        operator_title: str,
    ) -> str:
        if coverage_type == COVERAGE_GLOBAL:
            return "world"
        if country_code:
            return country_code.lower()
        slug = "".join(
            ch.lower() if ch.isalnum() else "-" for ch in operator_title.strip()
        ).strip("-")
        return slug or "unknown"

    @staticmethod
    def _extract_image_url(image: Any) -> str:
        if isinstance(image, dict):
            return str(image.get("url") or "").strip()
        if isinstance(image, str):
            return image.strip()
        return ""

    @staticmethod
    def _normalize_iso2(code: str) -> str:
        normalized = (code or "").upper().strip()
        if len(normalized) == 2 and normalized.isalpha():
            return normalized
        return ""

    @staticmethod
    def _primary_country_code(countries: list[dict[str, Any]]) -> str:
        if not countries:
            return ""
        return str(countries[0].get("country_code", "")).upper()

    @staticmethod
    def _covered_country_codes(countries: list[Any]) -> tuple[str, ...]:
        codes: list[str] = []
        for country in countries:
            if not isinstance(country, dict):
                continue
            code = str(country.get("country_code") or "").upper().strip()
            if code and code not in codes:
                codes.append(code)
        return tuple(codes)

    @staticmethod
    def _format_data_allowance(package: dict[str, Any]) -> str:
        if package.get("data"):
            return str(package["data"])
        if package.get("is_unlimited"):
            return "Unlimited"
        amount = package.get("amount")
        if amount is None:
            return ""
        try:
            mb = float(amount)
        except (TypeError, ValueError):
            return str(amount)
        if mb >= 1024 and mb % 1024 == 0:
            return f"{int(mb // 1024)} GB"
        if mb >= 1024:
            return f"{mb / 1024:g} GB"
        return f"{int(mb) if mb.is_integer() else mb} MB"

    @staticmethod
    def _extract_usd_price(package: dict[str, Any]) -> Decimal:
        prices = package.get("prices") or {}
        recommended = prices.get("recommended_retail_price") or {}
        if "USD" in recommended:
            return Decimal(str(recommended["USD"]))

        if package.get("price") is not None:
            return Decimal(str(package["price"]))

        return Decimal("0.00")

    @staticmethod
    def _extract_usd_net_price(package: dict[str, Any]) -> Decimal | None:
        prices = package.get("prices") or {}
        net_prices = prices.get("net_price") or {}
        if "USD" in net_prices:
            return Decimal(str(net_prices["USD"]))

        if package.get("net_price") is not None:
            return Decimal(str(package["net_price"]))

        return None

    @staticmethod
    def _optional_positive_int(value: Any) -> int | None:
        """Map Airalo voice/text: null → None; positive ints kept; 0/invalid → None."""
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    @staticmethod
    def _parse_voice_text_from_title(title: str) -> tuple[int | None, int | None]:
        """Fallback when Airalo omits package.voice/text but encodes them in the title."""
        voice_match = re.search(r"(\d+)\s*mins?\b", title, flags=re.IGNORECASE)
        text_match = re.search(r"(\d+)\s*sms\b", title, flags=re.IGNORECASE)
        voice = int(voice_match.group(1)) if voice_match else None
        text = int(text_match.group(1)) if text_match else None
        return voice, text


class AiraloOrderProvider:
    """Places eSIM orders via the Airalo Partner API."""

    def __init__(self, client: AiraloClient | None = None) -> None:
        self.client = client or AiraloClient()

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        payload = self.client.create_order(
            package_id=package_id,
            quantity=1,
            description=customer_ref,
        )
        return self._map_order(payload, customer_ref=customer_ref)

    def _map_order(self, payload: dict[str, Any], *, customer_ref: str) -> OrderResult:
        guides = payload.get("installation_guides") or {}
        guide_url = ""
        if isinstance(guides, dict):
            guide_url = str(guides.get("en") or "")

        sims: list[OrderedSimDTO] = []
        for sim in payload.get("sims") or []:
            if not isinstance(sim, dict):
                continue
            iccid = str(sim.get("iccid", "")).strip()
            if not iccid:
                continue
            sims.append(
                OrderedSimDTO(
                    iccid=iccid,
                    lpa=str(sim.get("lpa") or ""),
                    matching_id=str(sim.get("matching_id") or ""),
                    qrcode=str(sim.get("qrcode") or ""),
                    qrcode_url=str(sim.get("qrcode_url") or ""),
                    direct_apple_installation_url=str(
                        sim.get("direct_apple_installation_url") or ""
                    ),
                )
            )

        price = payload.get("price")
        return OrderResult(
            external_order_id=str(payload.get("id", "")),
            code=str(payload.get("code") or ""),
            package_id=str(payload.get("package_id") or ""),
            customer_ref=str(payload.get("description") or customer_ref),
            currency=str(payload.get("currency") or "USD"),
            price_usd=Decimal(str(price)) if price is not None else Decimal("0.00"),
            manual_installation=str(payload.get("manual_installation") or ""),
            qrcode_installation=str(payload.get("qrcode_installation") or ""),
            installation_guide_url=guide_url,
            sims=sims,
        )


class AiraloTopupProvider:
    """Lists top-ups, submits top-up orders, and fetches usage via Airalo."""

    def __init__(self, client: AiraloClient | None = None) -> None:
        self.client = client or AiraloClient()

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        items = self.client.list_topups(iccid)
        packages: list[TopupPackage] = []
        for item in items:
            mapped = self._map_topup_package(item)
            if mapped is not None:
                packages.append(mapped)
        return packages

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        payload = self.client.submit_topup(iccid=iccid, package_id=package_id)
        return self._map_topup_result(payload, iccid=iccid)

    def get_usage(self, iccid: str) -> UsageDTO:
        payload = self.client.get_usage(iccid)
        return self._map_usage(payload)

    def _map_topup_package(self, package: dict[str, Any]) -> TopupPackage | None:
        external_id = str(package.get("id", "")).strip()
        if not external_id:
            return None

        price = package.get("price")
        net_price = package.get("net_price")
        data_allowance = str(package.get("data") or "")
        if not data_allowance and package.get("is_unlimited"):
            data_allowance = "Unlimited"

        return TopupPackage(
            external_id=external_id,
            title=str(package.get("title") or ""),
            data_allowance=data_allowance,
            validity_days=int(package.get("day") or 0),
            price_usd=Decimal(str(price)) if price is not None else Decimal("0.00"),
            net_price_usd=Decimal(str(net_price)) if net_price is not None else None,
            is_unlimited=bool(package.get("is_unlimited", False)),
            plan_type=str(package.get("type") or "topup"),
        )

    def _map_topup_result(self, payload: dict[str, Any], *, iccid: str) -> TopupResult:
        price = payload.get("price")
        return TopupResult(
            external_order_id=str(payload.get("id", "")),
            code=str(payload.get("code") or ""),
            package_id=str(payload.get("package_id") or ""),
            iccid=iccid,
            currency=str(payload.get("currency") or "USD"),
            price_usd=Decimal(str(price)) if price is not None else Decimal("0.00"),
            customer_ref=str(payload.get("description") or ""),
        )

    @staticmethod
    def _map_usage(payload: dict[str, Any]) -> UsageDTO:
        is_unlimited = payload.get("is_unlimited")
        return UsageDTO(
            remaining_mb=int(payload.get("remaining") or 0),
            total_mb=int(payload.get("total") or 0),
            expired_at=(
                str(payload["expired_at"])
                if payload.get("expired_at") is not None
                else None
            ),
            is_unlimited=bool(is_unlimited) if is_unlimited is not None else None,
            status=str(payload.get("status") or "UNKNOWN"),
            remaining_voice=int(payload.get("remaining_voice") or 0),
            remaining_text=int(payload.get("remaining_text") or 0),
            total_voice=int(payload.get("total_voice") or 0),
            total_text=int(payload.get("total_text") or 0),
        )
