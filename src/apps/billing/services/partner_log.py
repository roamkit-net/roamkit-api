"""Structured partner logs. Identifiers only. No tokens, URLs, or bodies."""

from __future__ import annotations

import logging

logger = logging.getLogger("apps.billing.partner")


def log_partner_event(event: str, **fields: object) -> None:
    """One line: ``event key=value``. None values are omitted."""
    parts = [event]
    for key in sorted(fields):
        value = fields[key]
        if value is None:
            continue
        parts.append(f"{key}={value}")
    logger.info(" ".join(parts))


class PartnerLegacyUsageMixin:
    """Record that a legacy partner route was called. Does not change the response."""

    legacy_endpoint = ""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        user = getattr(request, "user", None)
        user_id = None
        if user is not None and getattr(user, "is_authenticated", False):
            user_id = user.pk
        status_code = getattr(response, "status_code", 0)
        log_partner_event(
            "partner.legacy_endpoint.used",
            endpoint=self.legacy_endpoint,
            method=getattr(request, "method", ""),
            user_id=user_id,
            status_class=f"{int(status_code) // 100}xx",
        )
        return response
