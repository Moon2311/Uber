from django.urls import path

from .views import FareEstimateView

urlpatterns = [
    path("estimate/", FareEstimateView.as_view(), name="fare-estimate"),
]
