"""Payment-provider abstraction. The rest of the app never talks to a provider SDK.

Contract every implementation must honour:
  * `idempotency_key` is passed to the provider on every money-moving call; the
    same key must never move money twice (that is what makes retries and
    reconciliation safe).
  * A call that may or may not have reached the provider raises ProviderTimeout
    (ambiguous) - never return "failed" unless the provider said so.
  * `lookup(idempotency_key)` answers "what happened to that request?" and
    returns None only if the provider never received it.
  * `parse_webhook` verifies the signature over the RAW body before parsing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal


class ProviderError(Exception):
    """Permanent problem with the request itself (bad token, key reuse...)."""


class ProviderTimeout(Exception):
    """No answer: the request may or may not have been processed (ambiguous)."""


class ProviderUnavailable(Exception):
    """Provider refused the connection: the request was NOT processed."""


class InvalidSignature(Exception):
    pass


@dataclass(frozen=True)
class ProviderResult:
    status: str  # succeeded | failed | pending
    reference: str = ""
    amount: Decimal | None = None
    currency: str | None = None
    failure_code: str = ""


@dataclass(frozen=True)
class MethodDetails:
    token: str
    brand: str
    last4: str
    exp_month: int | None
    exp_year: int | None


@dataclass(frozen=True)
class WebhookPayload:
    event_id: str
    event_type: str  # charge.succeeded | charge.failed | refund.succeeded | refund.failed
    idempotency_key: str
    reference: str
    amount: Decimal | None
    currency: str | None
    failure_code: str = ""
    raw: dict = field(default_factory=dict)  # the data object, for the audit row


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    def describe_method(self, token: str) -> MethodDetails:
        """Validate a client-side token and return display data (raises ProviderError)."""

    @abstractmethod
    def charge(self, *, amount: Decimal, currency: str, method_token: str,
               idempotency_key: str, metadata: dict) -> ProviderResult: ...  # fmt: skip

    @abstractmethod
    def refund(self, *, charge_reference: str, amount: Decimal, currency: str,
               idempotency_key: str) -> ProviderResult: ...  # fmt: skip

    @abstractmethod
    def lookup(self, *, idempotency_key: str) -> ProviderResult | None: ...

    @abstractmethod
    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookPayload: ...
