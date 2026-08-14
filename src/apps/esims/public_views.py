"""Public Matching ID status (ADR 022)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.esims.public_serializers import (
    CodedErrorSerializer,
    PublicEsimStatusRequestSerializer,
    PublicEsimStatusSerializer,
)
from apps.esims.services.public_status import (
    FORBIDDEN_REQUEST_KEYS,
    MatchingIdNotFoundError,
    get_public_esim_status,
)
from apps.esims.throttles import PublicEsimStatusRateThrottle

_INVALID = {"detail": "Invalid request.", "code": "invalid_request"}
_NOT_FOUND = {
    "detail": "Matching ID not found.",
    "code": "matching_id_not_found",
}


def _invalid_request() -> Response:
    return Response(_INVALID, status=status.HTTP_400_BAD_REQUEST)


def _not_found() -> Response:
    return Response(_NOT_FOUND, status=status.HTTP_404_NOT_FOUND)


@extend_schema_view(
    post=extend_schema(
        tags=["Public"],
        operation_id="public_esim_status",
        summary="Public Matching ID eSIM status",
        description=(
            "Read-only cache snapshot for a consumer Matching ID (ADR 022). "
            "Body must be a JSON object with only ``matching_id``. "
            "JWT is ignored and never widens the response. "
            "No provider refresh. Mutations are never authorized."
        ),
        request=PublicEsimStatusRequestSerializer,
        responses={
            200: OpenApiResponse(response=PublicEsimStatusSerializer),
            400: OpenApiResponse(response=CodedErrorSerializer),
            404: OpenApiResponse(response=CodedErrorSerializer),
            429: OpenApiResponse(response=CodedErrorSerializer),
        },
        auth=[],
    ),
)
class PublicEsimStatusView(APIView):
    """POST /api/v1/public/esim/status/ — Matching ID capability, cache only."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicEsimStatusRateThrottle]

    def handle_exception(self, exc):
        if isinstance(exc, (ParseError, ValidationError)):
            return _invalid_request()
        return super().handle_exception(exc)

    def post(self, request: Request) -> Response:
        raw = request.data
        if not isinstance(raw, dict):
            return _invalid_request()
        if any(key in raw for key in FORBIDDEN_REQUEST_KEYS):
            return _invalid_request()
        extra = set(raw) - {"matching_id"}
        if extra:
            return _invalid_request()
        matching_id = raw.get("matching_id")
        if matching_id is None:
            return _not_found()
        if not isinstance(matching_id, str):
            return _invalid_request()
        try:
            snapshot = get_public_esim_status(matching_id)
        except MatchingIdNotFoundError:
            return _not_found()
        return Response(PublicEsimStatusSerializer(snapshot.as_response_dict()).data)
