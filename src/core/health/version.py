"""Version / release metadata endpoint (ADR 013 must-have)."""

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def version(_request):
    """Non-secret build metadata for smoke tests and release verification."""
    payload = {
        "git_sha": getattr(settings, "ROAMKIT_GIT_SHA", "") or "",
        "build_date": getattr(settings, "ROAMKIT_BUILD_DATE", "") or "",
        "image_tag": getattr(settings, "ROAMKIT_IMAGE_TAG", "") or "",
        "environment": getattr(settings, "ROAMKIT_ENVIRONMENT", "") or "",
    }
    return JsonResponse(payload)
