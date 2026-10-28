import logging

from django.db import connection
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class HealthView(APIView):
    """Liveness + database readiness probe."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    @extend_schema(
        responses=inline_serializer(
            "Health",
            {
                "status": serializers.CharField(),
                "database": serializers.CharField(),
                "live_store": serializers.DictField(),
            },
        )
    )
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            db_status = "ok"
        except Exception:  # noqa: BLE001
            logger.exception("Health check: database unavailable")
            db_status = "unavailable"

        live = _live_store_health()
        if db_status != "ok":
            overall, code = "unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        elif live["status"] == "ok":
            overall, code = "ok", status.HTTP_200_OK
        elif live["status"] == "degraded":
            # Redis down but PostgreSQL fallback serving: stay in rotation.
            overall, code = "degraded", status.HTTP_200_OK
        else:
            overall, code = "unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        return Response({"status": overall, "database": db_status, "live_store": live}, status=code)


def _live_store_health() -> dict:
    from apps.locations.service import build_store

    try:
        store = build_store()
        return {"backend": store.name, **store.health()}
    except Exception as exc:  # noqa: BLE001 - health must never raise
        logger.exception("Health check: live store error")
        return {"status": "unavailable", "error": type(exc).__name__}
