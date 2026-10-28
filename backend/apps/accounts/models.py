from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    RIDER = "RIDER", "Rider"
    DRIVER = "DRIVER", "Driver"


class EmailUserManager(UserManager):
    """Email is the login identifier; `username` is kept equal to the email."""

    def _create_user(self, username, email, password, **extra_fields):
        email = self.normalize_email(email or username).lower()
        return super()._create_user(email, email, password, **extra_fields)

    def create_user(self, email=None, password=None, **extra_fields):
        extra_fields.pop("username", None)
        return super().create_user(email, email, password, **extra_fields)

    def create_superuser(self, email=None, password=None, **extra_fields):
        extra_fields.pop("username", None)
        extra_fields.setdefault("role", Role.RIDER)
        return super().create_superuser(email, email, password, **extra_fields)


class User(AbstractUser):
    # Kept in sync with `email` so Django's built-in auth machinery keeps working.
    username = models.CharField(max_length=254, unique=True)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, blank=True)
    # Role is assigned once at registration and is never writable via the public API.
    role = models.CharField(max_length=10, choices=Role.choices)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = EmailUserManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values), name="user_role_valid"
            ),
            models.UniqueConstraint(Lower("email"), name="user_email_ci_unique"),
        ]

    def __str__(self):
        return f"{self.email} ({self.role})"

    @property
    def is_rider(self) -> bool:
        return self.role == Role.RIDER

    @property
    def is_driver(self) -> bool:
        return self.role == Role.DRIVER
