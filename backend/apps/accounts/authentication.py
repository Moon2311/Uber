"""JWT authentication that loads the driver profile in the SAME query as the user.

Measured (docs/performance.md): permission checks (`IsDriver`) and most driver
endpoints touch `user.driver_profile`; with the stock authenticator that is a
second query on every GPS update, about 20% of its server time.
"""

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings


class JWTAuthenticationWithProfile(JWTAuthentication):
    def get_user(self, validated_token):
        try:
            user_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError as exc:
            raise InvalidToken("Token contained no recognizable user identification") from exc
        user = (
            self.user_model.objects.select_related("driver_profile")
            .filter(**{api_settings.USER_ID_FIELD: user_id})
            .first()
        )
        if user is None:
            raise AuthenticationFailed("User not found", code="user_not_found")
        if not user.is_active:
            raise AuthenticationFailed("User is inactive", code="user_inactive")
        return user


try:  # OpenAPI: document it exactly like the stock SimpleJWT bearer scheme
    from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme

    class JWTWithProfileScheme(SimpleJWTScheme):
        target_class = "apps.accounts.authentication.JWTAuthenticationWithProfile"
except ImportError:  # pragma: no cover
    pass
