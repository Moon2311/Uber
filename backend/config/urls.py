from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.common.observability import metrics_view

api_v1 = [
    path("auth/", include("apps.accounts.urls")),
    path("drivers/", include("apps.drivers.urls")),
    path("locations/", include("apps.locations.urls")),
    path("trips/", include("apps.trips.urls")),
    path("pricing/", include("apps.pricing.urls")),
    path("payments/", include("apps.payments.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("matching/", include("apps.matching.urls")),
    path("", include("apps.common.urls")),
]

urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("api/v1/", include((api_v1, "v1"))),
    path("metrics", metrics_view, name="metrics"),
]
if settings.API_DOCS_ENABLED:
    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    ]
