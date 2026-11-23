"""Typed error hierarchy. Services raise these; the API layer maps them to
problem+json responses (docs/design/08-api.md §1). We never swallow failures —
non-fatal issues surface as warnings on ApiResponse instead (golden rule #3)."""

from __future__ import annotations


class DVPError(Exception):
    """Base class for all platform errors."""

    code: str = "dvp_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DVPError):
    code = "not_found"
    http_status = 404


class ValidationError(DVPError):
    code = "validation_error"
    http_status = 422


class ConflictError(DVPError):
    code = "conflict"
    http_status = 409


class AuthError(DVPError):
    code = "unauthorized"
    http_status = 401


class AuthExpiredError(AuthError):
    """A data-source credential (e.g. Snowflake OAuth refresh token) expired.

    Raised instead of silently returning empty results, so callers surface a
    visible 're-authentication required' incident (docs/design/03 §7)."""

    code = "auth_expired"
    http_status = 401


class PermissionDeniedError(DVPError):
    code = "forbidden"
    http_status = 403
