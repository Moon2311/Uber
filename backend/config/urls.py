from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

api_v1 = [
    path("auth/", include("apps.accounts.urls")),
    path("drivers/", include("apps.drivers.urls")),
    path("locations/", include("apps.locations.urls")),
    path("trips/", include("apps.trips.urls")),
    path("pricing/", include("apps.pricing.urls")),
    path("", include("apps.common.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "v1"))),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
