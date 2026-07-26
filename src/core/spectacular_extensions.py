"""drf-spectacular authentication extension — JWT as ``bearerAuth``."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class SimpleJWTBearerAuthenticationScheme(OpenApiAuthenticationExtension):
    """Document SimpleJWT as HTTP Bearer (matches ``AUTH_HEADER_TYPES``)."""

    target_class = "rest_framework_simplejwt.authentication.JWTAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
