from django.urls import path

from .views import DeviceDetailView, DeviceListView, NotificationListView

urlpatterns = [
    path("", NotificationListView.as_view(), name="notifications"),
    path("devices/", DeviceListView.as_view(), name="devices"),
    path("devices/<uuid:device_id>/", DeviceDetailView.as_view(), name="device"),
]
