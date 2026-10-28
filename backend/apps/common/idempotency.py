"""Idempotency-Key support for unsafe POSTs (e.g. requesting a ride).

The first request with a key runs normally and its response is stored in the SAME
transaction as its side effects. A retry with the same key (and same body) gets
the stored response without re-executing; the same key with a different body is
rejected (422). Concurrent duplicates serialise on the caller's user row lock.
"""

import hashlib
import json
import re

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.accounts.models import User

from .models import IdempotencyRecord

HEADER = "HTTP_IDEMPOTENCY_KEY"
_VALID = re.compile(r"^[A-Za-z0-9_\-:.]{8,100}$")


class IdempotencyKeyReused(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "This Idempotency-Key was already used with a different request."
    default_code = "idempotency_key_reused"


def _hash(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def idempotent_response(request, scope: str, run) -> Response:
    """`run()` performs the operation and returns a Response."""
    key = request.META.get(HEADER)
    if key is None:
        return run()
    if not _VALID.match(key):
        raise ValidationError({"Idempotency-Key": ["8-100 chars of [A-Za-z0-9_-:.]"]})
    digest = _hash(request.data)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=request.user.pk)  # serialise duplicates
        record = IdempotencyRecord.objects.filter(user=request.user, scope=scope, key=key).first()
        if record is not None:
            if record.request_hash != digest:
                raise IdempotencyKeyReused()
            response = Response(record.response_body, status=record.response_status)
            response["Idempotent-Replayed"] = "true"
            return response
        response = run()
        if response.status_code < 500:
            IdempotencyRecord.objects.create(
                user=request.user, scope=scope, key=key, request_hash=digest,
                response_status=response.status_code,
                # Store exactly what the client received (Decimals etc. rendered).
                response_body=json.loads(JSONRenderer().render(response.data)),
            )  # fmt: skip
        return response
