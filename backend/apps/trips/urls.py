from django.urls import path

from .views import TripCancelView, TripCreateView, TripDetailView, TripHistoryView

urlpatterns = [
    path("", TripCreateView.as_view(), name="trip-create"),
    path("history/", TripHistoryView.as_view(), name="trip-history"),
    path("<uuid:trip_id>/", TripDetailView.as_view(), name="trip-detail"),
    path("<uuid:trip_id>/cancel/", TripCancelView.as_view(), name="trip-cancel"),
]
