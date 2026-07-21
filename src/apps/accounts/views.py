"""Auth API views."""

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.serializers import (
    ActivateSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserSerializer,
)
from apps.accounts.services.password_reset import GENERIC_PASSWORD_RESET_MESSAGE
from apps.accounts.services.registration import GENERIC_REGISTER_MESSAGE


class RegisterView(APIView):
    """Start registration with email only (sends confirmation link)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": GENERIC_REGISTER_MESSAGE},
            status=status.HTTP_200_OK,
        )


class ActivateView(APIView):
    """Set password from confirmation link and activate the account."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request) -> Response:
        serializer = ActivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)


class PasswordResetRequestView(APIView):
    """Request a password reset email (always returns a generic success)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request) -> Response:
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": GENERIC_PASSWORD_RESET_MESSAGE},
            status=status.HTTP_200_OK,
        )


class PasswordResetConfirmView(APIView):
    """Confirm password reset with uid + token and set a new password."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request) -> Response:
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "Password has been reset."},
            status=status.HTTP_200_OK,
        )


class MeView(APIView):
    """Return the authenticated user's profile."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)
