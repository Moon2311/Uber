"""HMAC webhook signatures: verified on raw bytes, with rotation and a replay window."""

import time

import pytest

from apps.payments.providers.base import InvalidSignature
from apps.payments.signing import compute, sign, verify

BODY = b'{"id":"evt_1","type":"charge.succeeded","data":{}}'


def test_valid_signature_passes():
    verify(BODY, sign(BODY))


def test_previous_secret_still_verifies_during_rotation():
    verify(BODY, sign(BODY, secret="whsec_test_previous"))


def test_several_v1_values_any_may_match():
    t = int(time.time())
    header = f"t={t},v1={'0' * 64},v1={compute('whsec_test_current', t, BODY)}"
    verify(BODY, header)


def _now():
    return int(time.time())


@pytest.mark.parametrize(
    "make, reason",
    [
        (lambda: (BODY + b" ", sign(BODY)), "mismatch"),  # one byte changed
        (lambda: (BODY, sign(BODY, secret="whsec_attacker")), "mismatch"),
        (lambda: (BODY, sign(BODY, timestamp=_now() - 301)), "tolerance"),  # replayed later
        (lambda: (BODY, sign(BODY, timestamp=_now() + 301)), "tolerance"),
        (lambda: (BODY, None), "missing"),
        (lambda: (BODY, ""), "missing"),
        (lambda: (BODY, "v1=abc"), "malformed"),
        (lambda: (BODY, "t=123"), "malformed"),
        (lambda: (BODY, "t=abc,v1=def"), "malformed"),
    ],
)
def test_bad_signatures_are_rejected(make, reason):
    body, header = make()
    with pytest.raises(InvalidSignature, match=reason):
        verify(body, header)


def test_signature_is_bound_to_the_timestamp():
    t = int(time.time())
    sig = compute("whsec_test_current", t, BODY)
    with pytest.raises(InvalidSignature):
        verify(BODY, f"t={t - 1},v1={sig}")  # moving t invalidates it


def test_no_configured_secret_rejects_everything(settings):
    header = sign(BODY)
    settings.PAYMENTS_WEBHOOK_SECRETS = []
    with pytest.raises(InvalidSignature, match="no webhook secret"):
        verify(BODY, header)
