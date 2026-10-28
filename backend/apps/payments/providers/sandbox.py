"""Sandbox payment provider: deterministic, provider-like, no network.

Behaviour is chosen by the client-side test token (as test cards are with real
providers); attaching it mints a reusable `pm_sandbox_<behaviour>_<id>`:

    tok_sandbox_visa, tok_sandbox_mastercard  -> succeeded
    tok_sandbox_decline                       -> failed (card_declined)
    tok_sandbox_insufficient_funds            -> failed (insufficient_funds)
    tok_sandbox_async                         -> pending; settled later by webhook
    tok_sandbox_timeout                       -> charge SUCCEEDS but the response is
                                                 lost (ProviderTimeout): the classic
                                                 ambiguous outcome
    tok_sandbox_lost_request                  -> first request never arrives
                                                 (ProviderTimeout); a retry with the
                                                 same idempotency key succeeds
    tok_sandbox_unavailable                   -> ProviderUnavailable (not processed)

State lives in the SandboxCharge table (standing in for the provider's database),
so idempotency keys and lookups behave like a real provider across processes.
Refused outside DEBUG unless PAYMENTS_SANDBOX_ALLOWED is set explicitly.
"""

from __future__ import annotations

import json
import secrets
from decimal import Decimal

from django.db import IntegrityError, transaction

from ..models import SandboxCharge
from ..signing import HEADER, sign, verify
from .base import (
    MethodDetails,
    PaymentProvider,
    ProviderError,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
    WebhookPayload,
)

PREFIX = "tok_sandbox_"
BRANDS = {"visa": ("visa", "4242"), "mastercard": ("mastercard", "4444")}
DECLINES = {"decline": "card_declined", "insufficient_funds": "insufficient_funds"}
KNOWN = {*BRANDS, *DECLINES, "async", "timeout", "lost_request", "unavailable"}


METHOD_PREFIX = "pm_sandbox_"  # reusable method id minted on attachment


def _behaviour(token: str) -> str:
    """Accepts a client-side test token (tok_sandbox_visa) or a minted reusable
    method id (pm_sandbox_visa_<hex>)."""
    if token.startswith(METHOD_PREFIX):
        behaviour = token[len(METHOD_PREFIX) :].rsplit("_", 1)[0]
    elif token.startswith(PREFIX):
        behaviour = token[len(PREFIX) :]
    else:
        behaviour = ""
    if behaviour not in KNOWN:
        raise ProviderError("unknown sandbox payment method")
    return behaviour


def _result(row: SandboxCharge) -> ProviderResult:
    return ProviderResult(status=row.status, reference=row.reference, amount=row.amount,
                          currency=row.currency, failure_code=row.failure_code)  # fmt: skip


class SandboxProvider(PaymentProvider):
    name = "sandbox"

    def describe_method(self, token: str) -> MethodDetails:
        behaviour = _behaviour(token)
        brand, last4 = BRANDS.get(behaviour, ("visa", "0002"))
        # Like real providers, attaching a one-time token yields a NEW reusable id.
        method_id = f"{METHOD_PREFIX}{behaviour}_{secrets.token_hex(6)}"
        return MethodDetails(token=method_id, brand=brand, last4=last4, exp_month=12,
                             exp_year=2030)  # fmt: skip

    def charge(self, *, amount, currency, method_token, idempotency_key, metadata):
        behaviour = _behaviour(method_token)
        if behaviour == "unavailable":
            raise ProviderUnavailable("sandbox: connection refused")
        existing = SandboxCharge.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.amount != amount or existing.currency != currency:
                raise ProviderError("idempotency key reused with different parameters")
            if existing.status != "not_received":
                return _result(existing)  # replay: same answer, no second charge
            existing.status = "succeeded"  # the retry of a lost request gets through
            existing.save(update_fields=["status"])
            return _result(existing)
        if behaviour == "lost_request":
            self._store("charge", idempotency_key, amount, currency, method_token, "not_received")
            raise ProviderTimeout("sandbox: request lost")
        status, code = "succeeded", ""
        if behaviour in DECLINES:
            status, code = "failed", DECLINES[behaviour]
        elif behaviour == "async":
            status = "pending"
        row = self._store("charge", idempotency_key, amount, currency, method_token, status, code)
        if behaviour == "timeout":
            raise ProviderTimeout("sandbox: response lost after processing")
        return _result(row)

    def refund(self, *, charge_reference, amount, currency, idempotency_key):
        existing = SandboxCharge.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return _result(existing)
        charge = SandboxCharge.objects.filter(reference=charge_reference, kind="charge").first()
        if charge is None or charge.status != "succeeded" or amount > charge.amount:
            row = self._store("refund", idempotency_key, amount, currency, "", "failed",
                              "charge_not_refundable", charge_reference)  # fmt: skip
        else:
            row = self._store("refund", idempotency_key, amount, currency, "", "succeeded", "",
                              charge_reference)  # fmt: skip
        return _result(row)

    def lookup(self, *, idempotency_key):
        row = SandboxCharge.objects.filter(idempotency_key=idempotency_key).first()
        if row is None or row.status == "not_received":
            return None
        return _result(row)

    def parse_webhook(self, body: bytes, headers):
        verify(body, headers.get(HEADER))
        try:
            event = json.loads(body)
            data = event["data"]
            return WebhookPayload(
                event_id=str(event["id"]), event_type=str(event["type"]),
                idempotency_key=str(data.get("idempotency_key", "")),
                reference=str(data.get("reference", "")),
                amount=Decimal(str(data["amount"])) if data.get("amount") is not None else None,
                currency=data.get("currency"), failure_code=str(data.get("failure_code", "")),
                raw=data,
            )  # fmt: skip
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("malformed webhook body") from exc

    # --- sandbox-only helpers ------------------------------------------------------
    @staticmethod
    def _store(kind, key, amount, currency, token, status, code="", charge_reference=""):
        try:
            with transaction.atomic():
                return SandboxCharge.objects.create(
                    reference=f"{'ch' if kind == 'charge' else 're'}_{secrets.token_hex(8)}",
                    idempotency_key=key, kind=kind, status=status, amount=amount,
                    currency=currency, token=token, failure_code=code,
                    charge_reference=charge_reference,
                )  # fmt: skip
        except IntegrityError:  # concurrent request with the same key
            return SandboxCharge.objects.get(idempotency_key=key)

    @staticmethod
    def settle_async(reference: str, outcome: str, *, failure_code: str = "card_declined",
                     event_id: str | None = None) -> tuple[bytes, dict]:  # fmt: skip
        """Finish a pending sandbox charge and build the signed webhook it would send."""
        row = SandboxCharge.objects.get(reference=reference)
        row.status = outcome
        row.failure_code = failure_code if outcome == "failed" else ""
        row.save(update_fields=["status", "failure_code"])
        return webhook_request(row, event_id=event_id)


def webhook_request(row: SandboxCharge, *, event_id=None, secret=None) -> tuple[bytes, dict]:
    body = json.dumps({
        "id": event_id or f"evt_{secrets.token_hex(8)}",
        "type": f"{row.kind}.{row.status}",
        "data": {"reference": row.reference, "idempotency_key": row.idempotency_key,
                 "amount": str(row.amount), "currency": row.currency,
                 "failure_code": row.failure_code},
    }).encode()  # fmt: skip
    return body, {HEADER: sign(body, secret=secret)}
