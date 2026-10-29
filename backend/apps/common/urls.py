from django.urls import path

from .views import EventsHealthView, HealthView, LiveView, ReadyView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("health/live/", LiveView.as_view(), name="health-live"),
    path("health/ready/", ReadyView.as_view(), name="health-ready"),
    path("health/events/", EventsHealthView.as_view(), name="health-events"),
]
