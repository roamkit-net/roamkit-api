"""Response guards for ops endpoints."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response


class NoStoreCacheMixin:
    """Prevent intermediary/browser caching of staff ops payloads."""

    def finalize_response(
        self,
        request: Request,
        response: Response,
        *args,
        **kwargs,
    ) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response
