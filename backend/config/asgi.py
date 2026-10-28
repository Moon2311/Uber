import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()  # initialise Django before importing consumers

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.realtime.auth import JwtAuthMiddleware, OriginGuard  # noqa: E402
from apps.realtime.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": OriginGuard(JwtAuthMiddleware(URLRouter(websocket_urlpatterns))),
    }
)
