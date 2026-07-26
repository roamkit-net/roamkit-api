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

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def create(self, validated_data: dict) -> dict:
        register_user(email=validated_data["email"])
        return validated_data


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


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "created_at", "updated_at")
        read_only_fields = fields


class GoogleAuthSerializer(serializers.Serializer):
    """GIS ID token credential from the browser."""

    credential = serializers.CharField(write_only=True, min_length=1, max_length=8192)


class GoogleAuthTokenResponseSerializer(serializers.Serializer):
    """Same JWT pair shape as password ``/auth/token/``."""

    access = serializers.CharField()
    refresh = serializers.CharField()


class GoogleAuthErrorSerializer(serializers.Serializer):
    """Locked Google auth error body (ADR 015)."""

    code = serializers.CharField()
    detail = serializers.CharField()
