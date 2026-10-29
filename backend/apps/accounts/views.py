from django.contrib.auth.models import update_last_login
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .serializers import (
    AuthResponseSerializer,
    LoginSerializer,
    RegisterSerializer,
    UserSerializer,
    tokens_for,
)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    @extend_schema(request=RegisterSerializer, responses={201: AuthResponseSerializer})
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"user": UserSerializer(user).data, **tokens_for(user)},
            status=status.HTTP_201_CREATED,
        )


class LoginEmailThrottle(SimpleRateThrottle):
    """Limits attempts against ONE account regardless of source IP (credential
    stuffing from many addresses). The key is a hash: no emails in cache keys."""

    scope = "auth_email"

    def get_cache_key(self, request, view):
        import hashlib

        email = str(request.data.get("email", "")).strip().lower()
        if not email:
            return None
        return self.cache_format % {
            "scope": self.scope, "ident": hashlib.sha256(email.encode()).hexdigest()[:32]
        }  # fmt: skip


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def get_throttles(self):
        from rest_framework.throttling import ScopedRateThrottle

        return [ScopedRateThrottle(), AnonRateThrottle(), LoginEmailThrottle()]

    @extend_schema(request=LoginSerializer, responses={200: AuthResponseSerializer})
    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        update_last_login(None, user)
        return Response({"user": UserSerializer(user).data, **tokens_for(user)})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=UserSerializer)
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class RefreshView(TokenRefreshView):
    throttle_scope = "auth"


class LogoutView(APIView):
    """Revokes the given refresh token (blacklist). The short-lived access token
    expires on its own (JWT_ACCESS_MINUTES)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={205: None})
    def post(self, request):
        try:
            token = RefreshToken(request.data.get("refresh", ""))
            if str(token.get("user_id")) != str(request.user.pk):  # claim is a string
                raise TokenError("not your token")
            token.blacklist()
        except TokenError:
            return Response(
                {"error": {"code": "invalid_token", "message": "Invalid token.", "details": {}}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_205_RESET_CONTENT)
