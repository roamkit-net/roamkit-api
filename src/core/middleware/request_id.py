"""Attach a correlation request id to every request/response."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin

from core.http.request_id import get_or_create_request_id


class RequestIdMiddleware(MiddlewareMixin):
    """Set ``request.request_id`` and echo ``X-Request-ID`` on the response."""

    def process_request(self, request: HttpRequest) -> None:
        get_or_create_request_id(request)

    def process_response(
        self, request: HttpRequest, response: HttpResponse
    ) -> HttpResponse:
        request_id = getattr(request, "request_id", None)
        if request_id and "X-Request-ID" not in response:
            response["X-Request-ID"] = str(request_id)
        return response
