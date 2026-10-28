from django.urls import path

from .views import AvailabilityView, NearbyDriversView

urlpatterns = [
    path("availability/", AvailabilityView.as_view(), name="driver-availability"),
    path("nearby/", NearbyDriversView.as_view(), name="drivers-nearby"),
]
