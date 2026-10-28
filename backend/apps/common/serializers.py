from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

_SIX_PLACES = Decimal("0.000001")


class _CoordinateField(serializers.DecimalField):
    """Decimal coordinate with range validation.

    Browsers report GPS coordinates with ~15 decimal places; we round to 6 (~0.1 m)
    rather than rejecting them. Range is still validated strictly.
    """

    bound: Decimal

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 9)
        kwargs.setdefault("decimal_places", 6)
        kwargs.setdefault("min_value", -self.bound)
        kwargs.setdefault("max_value", self.bound)
        kwargs.setdefault("coerce_to_string", False)
        super().__init__(**kwargs)

    def validate_precision(self, value):
        return super().validate_precision(value.quantize(_SIX_PLACES, rounding=ROUND_HALF_UP))


class LatitudeField(_CoordinateField):
    bound = Decimal("90")


class LongitudeField(_CoordinateField):
    bound = Decimal("180")
