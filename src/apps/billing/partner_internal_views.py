"""Internal join sign and consume. Not part of the public partner schema."""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services.partner_attribution import consume_partner_pending
from apps.billing.services.partner_invite import issue_join_signature
from apps.billing.services.partner_invite_visit import UTM_FIELDS

logger = logging.getLogger(__name__)
_NO_STORE = "no-store"


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


@extend_schema(exclude=True)
class PartnerJoinSignView(APIView):
    """Validate a join token and return a signed payload. Generic 404 otherwise."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        token = ""
        if isinstance(request.data, dict):
            raw = request.data.get("token", "")
            token = raw if isinstance(raw, str) else ""
        if not _allow_join(request):
            response = Response(status=status.HTTP_429_TOO_MANY_REQUESTS)
            response["Cache-Control"] = _NO_STORE
            return response
        signed = issue_join_signature(token, _utm(request))
        if signed is None:
            logger.info(
                "partner_join.rejected fingerprint=%s",
                _fingerprint(token) if token else "empty",
            )
            response = Response(status=status.HTTP_404_NOT_FOUND)
            response["Cache-Control"] = _NO_STORE
            return response
        response = Response({"payload": signed}, status=status.HTTP_200_OK)
        response["Cache-Control"] = _NO_STORE
        return response


@extend_schema(exclude=True)
class PartnerConsumeView(APIView):
    """Authenticated consume of a signed pending payload."""

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            response = Response(
                {"code": "authentication_required"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            response["Cache-Control"] = _NO_STORE
            return response
        return super().handle_exception(exc)

    def post(self, request: Request) -> Response:
        payload = ""
        if isinstance(request.data, dict):
            raw = request.data.get("payload", "")
            payload = raw if isinstance(raw, str) else ""
        result = consume_partner_pending(request.user, payload)
        response = Response({"status": result}, status=status.HTTP_200_OK)
        response["Cache-Control"] = _NO_STORE
        return response


def _utm(request: Request) -> dict[str, object]:
    if not isinstance(request.data, dict):
        return {}
    return {field: request.data.get(field, "") for field in UTM_FIELDS}


def _allow_join(request: Request) -> bool:
    limit = settings.PARTNER_JOIN_RATE_LIMIT
    ip = request.META.get("REMOTE_ADDR", "")
    key = f"partner-join:{ip}"
    count = cache.get(key, 0)
    if count >= limit:
        return False
    cache.set(key, count + 1, 3600)
    return True
