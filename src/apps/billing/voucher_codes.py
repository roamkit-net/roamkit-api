"""Voucher code normalization and reserved-code denylist (ADR 011)."""

from __future__ import annotations

import secrets
import unicodedata

# Case-insensitive after normalize; stored and compared in uppercase NFKC form.
RESERVED_VOUCHER_CODES: frozenset[str] = frozenset(
    {
        "ADMIN",
        "TEST",
        "FREE",
        "DEMO",
        "NULL",
        "SYSTEM",
    }
)


def normalize_voucher_code(raw: str) -> str:
    """Strip → Unicode NFKC → uppercase.

    `` summer2027``, ``Summer2027``, and ``SUMMER2027`` all become ``SUMMER2027``.
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw)).strip().upper()
    return text


def is_reserved_voucher_code(code: str) -> bool:
    """Return True if ``code`` (already normalized or raw) is reserved."""
    normalized = normalize_voucher_code(code)
    if not normalized:
        return False
    for reserved in RESERVED_VOUCHER_CODES:
        if secrets.compare_digest(normalized, reserved):
            return True
    return False


def assert_code_available(
    code: str, *, exclude_voucher_id=None, exclude_campaign_id=None
) -> str:
    """Normalize ``code``, reject reserved/empty, and ensure cross-table uniqueness.

    Returns the normalized code. Import models lazily to avoid circular imports.
    """
    from apps.billing.models import Voucher, VoucherCampaign

    normalized = normalize_voucher_code(code)
    if not normalized:
        raise ValueError("Voucher code must not be empty")
    if is_reserved_voucher_code(normalized):
        raise ValueError(f"Voucher code is reserved: {normalized}")

    voucher_qs = Voucher.objects.filter(code=normalized)
    if exclude_voucher_id is not None:
        voucher_qs = voucher_qs.exclude(pk=exclude_voucher_id)
    if voucher_qs.exists():
        raise ValueError(f"Voucher code already used: {normalized}")

    campaign_qs = VoucherCampaign.objects.filter(code=normalized)
    if exclude_campaign_id is not None:
        campaign_qs = campaign_qs.exclude(pk=exclude_campaign_id)
    if campaign_qs.exists():
        raise ValueError(f"Voucher code already used: {normalized}")

    return normalized
