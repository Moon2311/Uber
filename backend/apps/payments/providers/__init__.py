from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .base import (  # noqa: F401
    InvalidSignature,
    PaymentProvider,
    ProviderError,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)

PROVIDERS = {"sandbox": "apps.payments.providers.sandbox.SandboxProvider"}


def get_provider(name: str | None = None) -> PaymentProvider:
    name = name or settings.PAYMENTS_PROVIDER
    if name not in PROVIDERS:
        raise ImproperlyConfigured(f"Unknown PAYMENTS_PROVIDER {name!r}")
    if name == "sandbox" and not settings.PAYMENTS_SANDBOX_ALLOWED:
        raise ImproperlyConfigured("The sandbox payment provider is disabled in this environment")
    return import_string(PROVIDERS[name])()
