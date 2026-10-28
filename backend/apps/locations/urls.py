from django.urls import path

from .views import LocationUpdateView, MyLocationView

urlpatterns = [
    path("update/", LocationUpdateView.as_view(), name="location-update"),
    path("me/", MyLocationView.as_view(), name="location-me"),
]
