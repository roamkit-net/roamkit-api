"""Auth API views."""

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.serializers import (
    ActivateSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserSerializer,
)
from apps.accounts.services.human_verification.enforce import enforce_human_verification
from apps.accounts.services.password_reset import GENERIC_PASSWORD_RESET_MESSAGE
from apps.accounts.services.registration import GENERIC_REGISTER_MESSAGE
from apps.accounts.throttles import (
    AuthActivateRateThrottle,
    AuthPasswordResetConfirmRateThrottle,
    AuthPasswordResetRateThrottle,
    AuthRegisterRateThrottle,
    AuthTokenRateThrottle,
)
from core.openapi_serializers import DetailMessageSerializer, ErrorDetailSerializer


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_register",
        summary="Start registration",
        description=(
            "Begin registration with email only. Always returns a generic success "
            "message (no account enumeration)."
        ),
        auth=[],
        request=RegisterSerializer,
        responses={
            200: OpenApiResponse(
                response=DetailMessageSerializer,
                description="Generic registration acknowledgment",
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Validation error"
            ),
        },
    ),
)
class RegisterView(APIView):
    """Start registration with email only (sends confirmation link)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AuthRegisterRateThrottle]

    def post(self, request: Request) -> Response:
        enforce_human_verification(request, endpoint="auth_register")
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": GENERIC_REGISTER_MESSAGE},
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_activate",
        summary="Activate account",
        description="Set password from the confirmation link and activate the account.",
        auth=[],
        request=ActivateSerializer,
        responses={
            200: OpenApiResponse(response=UserSerializer, description="Activated user"),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid token or password"
            ),
        },
    ),
)
class ActivateView(APIView):
    """Set password from confirmation link and activate the account."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AuthActivateRateThrottle]

    def post(self, request: Request) -> Response:
        serializer = ActivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_password_reset_request",
        summary="Request password reset",
        description=(
            "Request a password reset email. Always returns a generic success message."
        ),
        auth=[],
        request=PasswordResetRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=DetailMessageSerializer,
                description="Generic password-reset acknowledgment",
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Validation error"
            ),
        },
    ),
)
class PasswordResetRequestView(APIView):
    """Request a password reset email (always returns a generic success)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AuthPasswordResetRateThrottle]

    def post(self, request: Request) -> Response:
        enforce_human_verification(request, endpoint="auth_password_reset")
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": GENERIC_PASSWORD_RESET_MESSAGE},
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_password_reset_confirm",
        summary="Confirm password reset",
        description="Confirm password reset with uid + token and set a new password.",
        auth=[],
        request=PasswordResetConfirmSerializer,
        responses={
            200: OpenApiResponse(
                response=DetailMessageSerializer, description="Password updated"
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid token or password"
            ),
        },
    ),
)
class PasswordResetConfirmView(APIView):
    """Confirm password reset with uid + token and set a new password."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AuthPasswordResetConfirmRateThrottle]

    def post(self, request: Request) -> Response:
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "Password has been reset."},
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Users"],
        operation_id="users_me",
        summary="Current user profile",
        description="Return the authenticated user's profile.",
        responses={
            200: OpenApiResponse(response=UserSerializer, description="User profile"),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
        },
    ),
)
class MeView(APIView):
    """Return the authenticated user's profile."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_login",
        summary="Obtain JWT tokens",
        description="Exchange email and password for access and refresh JWT tokens.",
        auth=[],
        request=TokenObtainPairSerializer,
        responses={
            200: OpenApiResponse(
                response=TokenObtainPairSerializer, description="JWT token pair"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid credentials"
            ),
        },
    ),
)
class AuthTokenObtainView(TokenObtainPairView):
    """POST /api/v1/auth/token/ — JWT obtain."""

    throttle_classes = [AuthTokenRateThrottle]

    def post(self, request: Request, *args, **kwargs) -> Response:
        enforce_human_verification(request, endpoint="auth_token")
        return super().post(request, *args, **kwargs)


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_refresh",
        summary="Refresh JWT access token",
        description="Exchange a refresh token for a new access token.",
        auth=[],
        request=TokenRefreshSerializer,
        responses={
            200: OpenApiResponse(
                response=TokenRefreshSerializer, description="New access token"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid refresh token"
            ),
        },
    ),
)
class AuthTokenRefreshView(TokenRefreshView):
    """POST /api/v1/auth/token/refresh/ — JWT refresh."""
