from django.urls import path

from .views import EventsHealthView, HealthView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("health/events/", EventsHealthView.as_view(), name="health-events"),
]
