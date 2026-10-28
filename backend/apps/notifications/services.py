"""Create notifications from events and deliver them with retries.

    Kafka event --(notifications consumer)--> Notification rows (deduplicated)
    worker: claim due rows (SKIP LOCKED + lease) -> send per device -> record

* Idempotent creation: dedupe_key = "<event_id>:<kind>:<user>" is unique, and the
  consumer's ProcessedEvent ledger commits in the same transaction.
* Retries: transient failures back off exponentially up to
  NOTIFICATIONS_MAX_ATTEMPTS; devices that already received it are skipped.
* At-least-once to the push service: a worker crash after sending but before
  recording may re-send once after the lease expires. The idempotency/collapse
  key lets the device show it once (APNs collapse-id / FCM collapse_key).
* Expired alerts are skipped rather than delivered late.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.metrics import metrics

from .models import (
    DeliveryStatus,
    DeviceToken,
    Notification,
    NotificationDelivery,
    NotificationStatus,
)
from .policy import plan
from .providers import (
    InvalidTokenError,
    PermanentDeliveryError,
    TransientDeliveryError,
    get_push_provider,
)

logger = logging.getLogger("apps.notifications")


def create_from_event(event) -> list[Notification]:
    created = []
    now = timezone.now()
    for item in plan(event):
        alert = item.alert
        collapse = (
            f"{alert.collapse_key}:{item.data.get('trip_id', '')}" if alert.collapse_key else ""
        )
        try:
            with transaction.atomic():
                created.append(Notification.objects.create(
                    user_id=item.user_id, kind=alert.kind, title=alert.title, body=alert.body,
                    data=item.data, dedupe_key=f"{event.event_id}:{alert.kind}:{item.user_id}",
                    collapse_key=collapse[:100], next_attempt_at=now,
                    expires_at=now + timedelta(seconds=alert.ttl_seconds),
                ))  # fmt: skip
        except IntegrityError:
            continue  # already created for this event
        metrics().increment("notifications.created", tags={"kind": alert.kind})
    return created


def handle_event(event) -> None:
    create_from_event(event)


HANDLERS = {
    "TripStatusChanged": handle_event,
    "RideOfferCreated": handle_event,
    "PaymentStatusChanged": handle_event,
}


def _backoff(attempts: int) -> float:
    return min(settings.NOTIFICATIONS_RETRY_MAX_SECONDS,
               settings.NOTIFICATIONS_RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))  # fmt: skip


def dispatch_due(*, limit: int = 50, provider=None, now=None) -> int:
    provider = provider or get_push_provider()
    now = now or timezone.now()
    with transaction.atomic():
        due = list(
            Notification.objects.select_for_update(skip_locked=True)
            .filter(status=NotificationStatus.PENDING, next_attempt_at__lte=now)
            .order_by("next_attempt_at")[:limit]
        )
        lease = now + timedelta(seconds=settings.NOTIFICATIONS_LEASE_SECONDS)
        for n in due:
            n.attempts += 1
            n.next_attempt_at = lease  # crash-safe: retried after the lease
            n.save(update_fields=["attempts", "next_attempt_at"])
    for n in due:
        deliver(n, provider=provider, now=now)
    return len(due)


def _finish(n: Notification, status: str, error: str = "", **extra) -> None:
    Notification.objects.filter(pk=n.pk).update(status=status, last_error=error[:255], **extra)
    metrics().increment("notifications.finished", tags={"kind": n.kind, "status": status})


def deliver(n: Notification, *, provider, now) -> str:
    if n.expires_at <= now:
        _finish(n, NotificationStatus.SKIPPED, "expired")
        return NotificationStatus.SKIPPED
    devices = list(DeviceToken.objects.filter(user_id=n.user_id, is_active=True))
    if not devices:
        _finish(n, NotificationStatus.SKIPPED, "no_devices")
        return NotificationStatus.SKIPPED
    done = set(
        n.deliveries.filter(status__in=[DeliveryStatus.SENT, DeliveryStatus.INVALID_TOKEN,
                                        DeliveryStatus.FAILED])
        .values_list("device_id", flat=True)
    )  # fmt: skip
    transient = []
    for device in devices:
        if device.pk in done:
            continue
        status, message_id, error = DeliveryStatus.SENT, "", ""
        try:
            message_id = provider.send(
                token=device.token, platform=device.platform, title=n.title, body=n.body,
                data={**n.data, "notification_id": str(n.pk), "kind": n.kind},
                collapse_key=n.collapse_key, idempotency_key=f"{n.pk}:{device.pk}",
            )  # fmt: skip
        except TransientDeliveryError as exc:
            status, error = DeliveryStatus.RETRYING, str(exc)
            transient.append(error)
        except InvalidTokenError as exc:
            status, error = DeliveryStatus.INVALID_TOKEN, str(exc)
            DeviceToken.objects.filter(pk=device.pk).update(is_active=False)
        except PermanentDeliveryError as exc:
            status, error = DeliveryStatus.FAILED, str(exc)
        delivery, _ = NotificationDelivery.objects.get_or_create(
            notification=n, device=device, defaults={"status": status}
        )
        delivery.status, delivery.provider_message_id = status, message_id
        delivery.error, delivery.attempts = error[:255], delivery.attempts + 1
        delivery.save()
        metrics().increment("notifications.delivery", tags={"status": status})

    sent_any = n.deliveries.filter(status=DeliveryStatus.SENT).exists()
    if transient and n.attempts < settings.NOTIFICATIONS_MAX_ATTEMPTS:
        retry_at = now + timedelta(seconds=_backoff(n.attempts))
        Notification.objects.filter(pk=n.pk).update(
            next_attempt_at=retry_at, last_error=transient[0][:255],
            **({"sent_at": now} if sent_any and n.sent_at is None else {}),
        )  # fmt: skip
        return NotificationStatus.PENDING
    if sent_any:
        _finish(n, NotificationStatus.SENT, transient[0] if transient else "",
                sent_at=n.sent_at or now)  # fmt: skip
        return NotificationStatus.SENT
    reason = transient[0] if transient else "no deliverable device"
    _finish(n, NotificationStatus.FAILED, reason)
    logger.warning("notifications.failed", extra={"notification_id": str(n.pk), "kind": n.kind,
                                                  "attempts": n.attempts})  # fmt: skip
    return NotificationStatus.FAILED
