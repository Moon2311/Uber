import logging

from django.db import transaction
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.guards import run_guards
from apps.common.permissions import IsRider

from .models import PaymentRecord, SavedPaymentMethod
from .providers import InvalidSignature, ProviderError, get_provider
from .serializers import (
    AddPaymentMethodSerializer,
    PaymentMethodSerializer,
    PaymentSerializer,
    RetryPaymentSerializer,
    StaffPaymentSerializer,
)
from .services import process_webhook, refund_payment, retry_payment

logger = logging.getLogger("apps.payments")


class PaymentMethodListView(APIView):
    permission_classes = [IsRider]

    @extend_schema(responses=PaymentMethodSerializer(many=True))
    def get(self, request):
        methods = SavedPaymentMethod.objects.filter(user=request.user, is_active=True)
        return Response(PaymentMethodSerializer(methods, many=True).data)

    @extend_schema(request=AddPaymentMethodSerializer, responses={201: PaymentMethodSerializer})
    def post(self, request):
        serializer = AddPaymentMethodSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        provider = get_provider()
        try:
            details = provider.describe_method(serializer.validated_data["provider_token"])
        except ProviderError as exc:
            raise ValidationError({"provider_token": ["Invalid payment method token."]}) from exc
        with transaction.atomic():
            run_guards("add_payment_method", user=request.user)  # card-testing protection
            existing = SavedPaymentMethod.objects.filter(
                provider=provider.name, provider_token=details.token
            ).first()
            if existing is not None and existing.user_id != request.user.pk:
                raise ValidationError({"provider_token": ["Invalid payment method token."]})
            mine = SavedPaymentMethod.objects.select_for_update().filter(user=request.user)
            make_default = serializer.validated_data["make_default"] or not mine.filter(
                is_active=True).exists()  # fmt: skip
            if make_default:
                mine.filter(is_default=True).update(is_default=False)
            method, _ = SavedPaymentMethod.objects.update_or_create(
                provider=provider.name, provider_token=details.token,
                defaults={"user": request.user, "brand": details.brand, "last4": details.last4,
                          "exp_month": details.exp_month, "exp_year": details.exp_year,
                          "is_active": True, "is_default": make_default},
            )  # fmt: skip
        return Response(PaymentMethodSerializer(method).data, status=status.HTTP_201_CREATED)


class PaymentMethodDetailView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=None, responses={204: None})
    def delete(self, request, method_id):
        updated = SavedPaymentMethod.objects.filter(
            pk=method_id, user=request.user, is_active=True
        ).update(is_active=False, is_default=False)
        if not updated:
            raise Http404
        return Response(status=status.HTTP_204_NO_CONTENT)


class TripPaymentView(generics.RetrieveAPIView):
    """The payment for one of the caller's trips (riders only see their own)."""

    permission_classes = [IsAuthenticated]
    lookup_url_kwarg = "trip_id"
    lookup_field = "trip_id"

    def get_serializer_class(self):
        return StaffPaymentSerializer if self.request.user.is_staff else PaymentSerializer

    def get_queryset(self):
        qs = PaymentRecord.objects.select_related("payment_method")
        return qs if self.request.user.is_staff else qs.filter(rider=self.request.user)


class RetryPaymentView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=RetryPaymentSerializer, responses=PaymentSerializer)
    def post(self, request, payment_id):
        serializer = RetryPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payment = retry_payment(payment_id=payment_id, rider=request.user,
                                    **serializer.validated_data)  # fmt: skip
        except PaymentRecord.DoesNotExist as exc:
            raise Http404 from exc
        return Response(PaymentSerializer(payment).data)


class RefundPaymentView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=None, responses=StaffPaymentSerializer)
    def post(self, request, payment_id):
        if not PaymentRecord.objects.filter(pk=payment_id).exists():
            raise Http404
        payment = refund_payment(payment_id=payment_id)
        logger.info("payments.refund.requested", extra={"payment_id": str(payment_id),
                                                        "staff_id": request.user.pk})  # fmt: skip
        return Response(StaffPaymentSerializer(payment).data)


@method_decorator(csrf_exempt, name="dispatch")
class WebhookView(APIView):
    """Provider callbacks. No user authentication: the HMAC signature over the raw
    body is the authentication. 400 for anything unverifiable (the provider will
    retry legitimately signed events; forged ones get nothing useful back)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(request=None, responses={200: dict, 400: dict})
    def post(self, request, provider):
        body = request.body  # raw bytes, read BEFORE any parsing
        try:
            outcome = process_webhook(provider, body, request.headers)
        except InvalidSignature as exc:
            logger.warning("payments.webhook.invalid_signature",
                           extra={"provider": provider, "reason": str(exc)})  # fmt: skip
            return Response({"error": {"code": "invalid_signature", "message": "Rejected.",
                                       "details": {}}}, status=400)  # fmt: skip
        except ProviderError:
            return Response({"error": {"code": "invalid_payload", "message": "Rejected.",
                                       "details": {}}}, status=400)  # fmt: skip
        return Response({"status": outcome})
