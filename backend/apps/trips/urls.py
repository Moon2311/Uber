from django.urls import path

from .views import (
    TripCancelView,
    TripCreateView,
    TripDetailView,
    TripHistoryView,
    TripMilestoneView,
)

urlpatterns = [
    path("", TripCreateView.as_view(), name="trip-create"),
    path("history/", TripHistoryView.as_view(), name="trip-history"),
    path("<uuid:trip_id>/", TripDetailView.as_view(), name="trip-detail"),
    path("<uuid:trip_id>/cancel/", TripCancelView.as_view(), name="trip-cancel"),
    path(
        "<uuid:trip_id>/arrive/",
        TripMilestoneView.as_view(milestone="arrive"),
        name="trip-arrive",
    ),
    path("<uuid:trip_id>/start/", TripMilestoneView.as_view(milestone="start"), name="trip-start"),
    path(
        "<uuid:trip_id>/complete/",
        TripMilestoneView.as_view(milestone="complete"),
        name="trip-complete",
    ),
]
