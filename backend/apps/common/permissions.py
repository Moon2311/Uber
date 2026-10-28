"""Role-based permissions.

The role is always read from the authenticated user record in the database (via the
JWT `user_id` claim) – never from request bodies, headers or token payload fields.
"""

from rest_framework.permissions import BasePermission

from apps.accounts.models import Role


class IsRider(BasePermission):
    message = "Only riders can perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role == Role.RIDER)


class IsDriver(BasePermission):
    message = "Only drivers can perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.role == Role.DRIVER
            and hasattr(user, "driver_profile")
        )
