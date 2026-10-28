"""WebSocket authentication and origin checks.

Token transport: the JWT access token travels in the WebSocket *subprotocol* list,
    new WebSocket(url, ["jwt", accessToken])
not in the URL (URLs end up in proxy/access logs). The server answers with the
"jwt" subprotocol. Browsers cannot attach Authorization headers to WebSockets,
and because auth is not cookie-based, cross-site WebSocket hijacking cannot ride
on a victim's session; the Origin allow-list is defence in depth.
"""

from __future__ import annotations

from urllib.parse import urlparse

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

SUBPROTOCOL = "jwt"


def token_from_scope(scope) -> str | None:
    protocols = scope.get("subprotocols") or []
    if len(protocols) >= 2 and protocols[0] == SUBPROTOCOL:
        return protocols[1]
    return None


@database_sync_to_async
def _user_for(user_id):
    from apps.accounts.models import User

    return User.objects.select_related("driver_profile").filter(pk=user_id, is_active=True).first()


class JwtAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope, user=AnonymousUser(), token_exp=None, auth_error=None)
        raw = token_from_scope(scope)
        if raw is None:
            scope["auth_error"] = "missing_token"
        else:
            try:
                token = AccessToken(raw)  # verifies signature, type and expiry
                user = await _user_for(token["user_id"])
                if user is None:
                    scope["auth_error"] = "unknown_user"
                else:
                    scope["user"], scope["token_exp"] = user, int(token["exp"])
            except (TokenError, KeyError):
                scope["auth_error"] = "invalid_token"
        return await self.app(scope, receive, send)


class OriginGuard:
    """Rejects browser connections from origins not in REALTIME_ALLOWED_ORIGINS.
    Native apps send no Origin; allowed when REALTIME_ALLOW_MISSING_ORIGIN."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        origin = dict(scope.get("headers") or []).get(b"origin")
        if not self._allowed(origin.decode("latin1") if origin else None):
            # Reject the handshake before any application code runs.
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 4403})
            return None
        return await self.app(scope, receive, send)

    @staticmethod
    def _allowed(origin: str | None) -> bool:
        if origin is None:
            return settings.REALTIME_ALLOW_MISSING_ORIGIN
        parsed = urlparse(origin)
        normalized = f"{parsed.scheme}://{parsed.netloc}".lower()
        return normalized in {o.lower().rstrip("/") for o in settings.REALTIME_ALLOWED_ORIGINS}
