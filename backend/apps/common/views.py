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
            {"status": serializers.CharField(), "database": serializers.CharField()},
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

        healthy = db_status == "ok"
        return Response(
            {"status": "ok" if healthy else "degraded", "database": db_status},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
