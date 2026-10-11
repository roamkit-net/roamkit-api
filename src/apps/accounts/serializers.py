"""Auth serializers."""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.services.password_reset import (
    PasswordResetError,
    confirm_password_reset,
    request_password_reset,
)
from apps.accounts.services.registration import (
    ActivationError,
    activate_user,
    register_user,
)
from apps.accounts.services.signup import (
    SignupCodeError,
    confirm_signup,
    password_rule_errors,
    resend_signup_code,
    start_signup,
)

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def create(self, validated_data: dict):
        return register_user(
            email=validated_data["email"],
            partner_pending=self.context.get("partner_pending"),
        )


class AccountExistsSerializer(serializers.Serializer):
    """Verified-invite response when the email already has an active account."""

    code = serializers.ChoiceField(choices=["account_exists"])
    detail = serializers.CharField()


class ActivateSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    password_confirm = serializers.CharField(
        write_only=True,
        min_length=8,
        max_length=128,
    )

    def create(self, validated_data: dict) -> User:
        try:
            return activate_user(
                uid=validated_data["uid"],
                token=validated_data["token"],
                password=validated_data["password"],
                password_confirm=validated_data["password_confirm"],
            )
        except ActivationError as exc:
            field = exc.field or "non_field_errors"
            raise serializers.ValidationError({field: exc.message}) from exc


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def create(self, validated_data: dict) -> dict:
        request_password_reset(email=validated_data["email"])
        return validated_data


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    password_confirm = serializers.CharField(
        write_only=True,
        min_length=8,
        max_length=128,
    )

    def create(self, validated_data: dict) -> User:
        try:
            return confirm_password_reset(
                uid=validated_data["uid"],
                token=validated_data["token"],
                password=validated_data["password"],
                password_confirm=validated_data["password_confirm"],
            )
        except PasswordResetError as exc:
            field = exc.field or "non_field_errors"
            raise serializers.ValidationError({field: exc.message}) from exc


class SignupSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100, trim_whitespace=True)
    last_name = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default="",
    )
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, max_length=128)

    def validate_first_name(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("This field may not be blank.")
        return cleaned

    def validate_password(self, value: str) -> str:
        errors = password_rule_errors(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value

    def create(self, validated_data: dict) -> dict:
        start_signup(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data.get("last_name") or "",
        )
        return validated_data


class SignupConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.RegexField(r"^\d{4}$")

    def create(self, validated_data: dict) -> User:
        try:
            return confirm_signup(
                email=validated_data["email"],
                code=validated_data["code"],
            )
        except SignupCodeError as exc:
            raise serializers.ValidationError(
                {"non_field_errors": exc.message}
            ) from exc


class SignupResendSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def create(self, validated_data: dict) -> dict:
        resend_signup_code(email=validated_data["email"])
        return validated_data


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "display_name",
            "is_staff",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class MeDisplayNameSerializer(serializers.Serializer):
    """The only field a customer may change on their own profile."""

    display_name = serializers.CharField(
        max_length=255,
        allow_blank=True,
        trim_whitespace=True,
    )

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            extra = set(data.keys()) - {"display_name"}
            if extra:
                raise serializers.ValidationError(
                    {
                        field: ["This field cannot be changed."]
                        for field in sorted(extra)
                    }
                )
        return super().to_internal_value(data)


class GoogleAuthSerializer(serializers.Serializer):
    """GIS ID token credential from the browser."""

    credential = serializers.CharField(write_only=True, min_length=1, max_length=8192)


class AuthTokenResponseSerializer(serializers.Serializer):
    """JWT pair shared by Google sign-in and password-reset confirm."""

    access = serializers.CharField()
    refresh = serializers.CharField()


class GoogleAuthErrorSerializer(serializers.Serializer):
    """Locked Google auth error body (ADR 015)."""

    code = serializers.CharField()
    detail = serializers.CharField()
