"""Consistent error envelope for every API error.

Shape:
    {"error": {"code": "<machine_code>", "message": "<human message>", "details": {...}}}
"""

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import IntegrityError, OperationalError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class ConflictError(exceptions.APIException):
    """The request is valid but conflicts with the current state of a resource."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


class ServiceUnavailableError(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "A backing service is temporarily unavailable. Please retry."
    default_code = "service_unavailable"


def _error_body(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def api_exception_handler(exc, context):
    from apps.locations.stores import StoreUnavailable

    # A denial that must be audited even though its transaction rolled back
    # (e.g. fraud BLOCK decisions). Runs here, after the atomic blocks exited.
    if callable(record := getattr(exc, "record_after_rollback", None)):
        try:
            record()
        except Exception:  # noqa: BLE001 - auditing must not turn a 403 into a 500
            logger.exception("Could not record rejected action")

    if isinstance(exc, StoreUnavailable):
        # Only reached when no fallback is configured (LOCATION_STORE_FALLBACK=none).
        logger.error("Live store unavailable: %s", exc)
        exc = ServiceUnavailableError()
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    elif isinstance(exc, OperationalError):
        # Database down, lock_timeout / statement_timeout hit: transient and safe to
        # retry (every write is transactional and idempotent where it matters).
        logger.warning("Database unavailable or timed out: %s", type(exc).__name__)
        response = Response(
            _error_body("service_unavailable", "Temporarily unavailable. Please retry."),
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response["Retry-After"] = "2"
        return response
    elif isinstance(exc, IntegrityError):
        # A DB constraint caught something validation missed (e.g. a race).
        logger.warning("Integrity error in %s", context.get("view").__class__.__name__)
        return Response(
            _error_body("conflict", "The request violates a data integrity constraint."),
            status=status.HTTP_409_CONFLICT,
        )

    response = exception_handler(exc, context)
    if response is None:
        return None  # unhandled -> Django 500 (logged, never leaked)

    if isinstance(exc, exceptions.ValidationError):
        details = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        response.data = _error_body("validation_error", "Invalid input.", details)
    else:
        codes = exc.get_codes() if isinstance(exc, exceptions.APIException) else "error"
        code = codes if isinstance(codes, str) else "error"
        detail = getattr(exc, "detail", str(exc))
        message = str(detail) if not isinstance(detail, (dict, list)) else "Request failed."
        details = detail if isinstance(detail, (dict, list)) else {}
        response.data = _error_body(code, message, details)
    return response
