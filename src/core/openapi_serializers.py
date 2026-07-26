"""Shared OpenAPI / DRF serializers for schema documentation (no runtime behavior)."""

from __future__ import annotations

from rest_framework import serializers


class ErrorDetailSerializer(serializers.Serializer):
    """Standard DRF ``{"detail": "..."}`` error body."""

    detail = serializers.CharField()


class InsufficientCreditsSerializer(serializers.Serializer):
    """Structured 402 payload from ``InsufficientFundsError.to_api_dict()``."""

    code = serializers.CharField()
    detail = serializers.CharField()
    required = serializers.CharField(required=False)
    balance = serializers.CharField(required=False)
    missing = serializers.CharField(required=False)


class DetailMessageSerializer(serializers.Serializer):
    """Generic success message with a ``detail`` string."""

    detail = serializers.CharField()
