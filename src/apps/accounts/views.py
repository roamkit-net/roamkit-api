"""Auth API views."""

from django.conf import settings
from django.contrib.auth import get_user_model
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
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

from apps.accounts.providers.google import authenticate_with_google
from apps.accounts.providers.google.errors import GoogleAuthError, GoogleAuthErrorCode
from apps.accounts.serializers import (
    ActivateSerializer,
    GoogleAuthErrorSerializer,
    GoogleAuthSerializer,
    GoogleAuthTokenResponseSerializer,
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
    AuthGoogleRateThrottle,
    AuthPasswordResetConfirmRateThrottle,
    AuthPasswordResetRateThrottle,
    AuthRegisterRateThrottle,
    AuthTokenRateThrottle,
)
from core.openapi_serializers import DetailMessageSerializer, ErrorDetailSerializer

User = get_user_model()


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
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.user
        User.objects.filter(pk=user.pk).update(
            last_login_provider=User.LastLoginProvider.PASSWORD
        )
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


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


@extend_schema_view(
    post=extend_schema(
        tags=["Authentication"],
        operation_id="auth_google",
        summary="Sign in with Google (GIS ID token)",
        description=(
            "Verify a Google Identity Services ID token and return the same "
            "SimpleJWT access/refresh pair as password login (ADR 015)."
        ),
        auth=[],
        request=GoogleAuthSerializer,
        examples=[
            OpenApiExample(
                "Request",
                value={"credential": "<google_id_token>"},
                request_only=True,
            ),
            OpenApiExample(
                "Success",
                value={"access": "eyJ...", "refresh": "eyJ..."},
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                "Invalid token",
                value={
                    "code": "google_invalid_token",
                    "detail": "Invalid Google credential.",
                },
                response_only=True,
                status_codes=["400"],
            ),
            OpenApiExample(
                "Email not verified",
                value={
                    "code": "google_email_not_verified",
                    "detail": "Google account email is not verified.",
                },
                response_only=True,
                status_codes=["400"],
            ),
            OpenApiExample(
                "Account disabled",
                value={
                    "code": "google_account_disabled",
                    "detail": "This account is disabled.",
                },
                response_only=True,
                status_codes=["401"],
            ),
            OpenApiExample(
                "Feature disabled",
                value={"code": "google_feature_disabled", "detail": "Not found."},
                response_only=True,
                status_codes=["404"],
            ),
            OpenApiExample(
                "Sub conflict",
                value={
                    "code": "google_sub_conflict",
                    "detail": "Google account is already linked.",
                },
                response_only=True,
                status_codes=["409"],
            ),
            OpenApiExample(
                "Verify unavailable",
                value={
                    "code": "google_verify_unavailable",
                    "detail": (
                        "Google sign-in is temporarily unavailable. Please try again."
                    ),
                },
                response_only=True,
                status_codes=["503"],
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=GoogleAuthTokenResponseSerializer,
                description="JWT token pair",
            ),
            400: OpenApiResponse(
                response=GoogleAuthErrorSerializer, description="Invalid credential"
            ),
            401: OpenApiResponse(
                response=GoogleAuthErrorSerializer, description="Account disabled"
            ),
            404: OpenApiResponse(
                response=GoogleAuthErrorSerializer, description="Feature disabled"
            ),
            409: OpenApiResponse(
                response=GoogleAuthErrorSerializer, description="Google sub conflict"
            ),
            503: OpenApiResponse(
                response=GoogleAuthErrorSerializer,
                description="Google verify unavailable",
            ),
        },
    ),
)
class GoogleAuthView(APIView):
    """POST /api/v1/auth/google/ — GIS ID token → SimpleJWT."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AuthGoogleRateThrottle]

    def post(self, request: Request) -> Response:
        if not getattr(settings, "GOOGLE_OAUTH_ENABLED", False):
            raise GoogleAuthError(GoogleAuthErrorCode.FEATURE_DISABLED)

        serializer = GoogleAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = authenticate_with_google(
            credential=serializer.validated_data["credential"]
        )
        return Response(
            {"access": result.access, "refresh": result.refresh},
            status=status.HTTP_200_OK,
        )
