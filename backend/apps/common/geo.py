"""Geographic helpers. Replaced by H3 indexing for search in a later step."""

import math
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator

EARTH_RADIUS_KM = 6371.0088

LATITUDE_VALIDATORS = [MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))]
LONGITUDE_VALIDATORS = [MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))]

# 6 decimal places ~= 0.11 m precision; 9 digits fits -180.000000..180.000000
COORDINATE_FIELD_KWARGS = {"max_digits": 9, "decimal_places": 6}


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bounding_box(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lng, max_lng) enclosing a circle.

    Used as a cheap indexed pre-filter before exact haversine filtering.
    """
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    cos_lat = max(math.cos(math.radians(lat)), 1e-12)
    dlng = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    return (
        max(lat - dlat, -90.0),
        min(lat + dlat, 90.0),
        max(lng - dlng, -180.0),
        min(lng + dlng, 180.0),
    )
