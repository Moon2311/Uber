import re

from rest_framework import serializers

from .models import PaymentRecord, SavedPaymentMethod

_DIGITS = re.compile(r"[\s-]")


def _looks_like_card_number(value: str) -> bool:
    digits = _DIGITS.sub("", value)
    return digits.isdigit() and 12 <= len(digits) <= 19


class _Strict(serializers.Serializer):
    def validate(self, attrs):
        # Unknown fields (card_number, cvc, ...) are rejected, never silently dropped.
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return attrs


class AddPaymentMethodSerializer(_Strict):
    """Clients tokenize cards with the provider SDK and send only the token."""

    provider_token = serializers.CharField(max_length=255)
    make_default = serializers.BooleanField(default=True)

    def validate_provider_token(self, value):
        if _looks_like_card_number(value):
            # Deliberately generic and value-free: the input must never be echoed.
            raise serializers.ValidationError(
                "Card numbers are never accepted. Tokenize the card with the payment SDK."
            )
        return value


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedPaymentMethod
        fields = ["id", "brand", "last4", "exp_month", "exp_year", "is_default", "created_at"]
        read_only_fields = fields  # provider_token is never serialized


class RetryPaymentSerializer(_Strict):
    payment_method_id = serializers.UUIDField(required=False, default=None)


class PaymentSerializer(serializers.ModelSerializer):
    card = serializers.SerializerMethodField()

    class Meta:
        model = PaymentRecord
        fields = ["id", "trip", "status", "amount", "currency", "method", "card", "failure_code",
                  "paid_at", "failed_at", "refunded_at", "created_at", "updated_at"]  # fmt: skip
        read_only_fields = fields

    def get_card(self, obj) -> dict | None:
        m = obj.payment_method
        return {"brand": m.brand, "last4": m.last4} if m else None


class StaffPaymentSerializer(PaymentSerializer):
    class Meta(PaymentSerializer.Meta):
        fields = PaymentSerializer.Meta.fields + ["provider", "provider_reference",
                                                  "needs_review", "review_reason"]  # fmt: skip
        read_only_fields = fields
