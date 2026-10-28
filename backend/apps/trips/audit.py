"""Trip audit log writer. Callers are always inside the transaction that made the
change, so an audit row exists if and only if the change committed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from .models import ActorType, TripEvent

if TYPE_CHECKING:
    from .state_machine import Actor


def record(
    trip,
    event_type: str,
    *,
    actor: Actor | None = None,
    from_status: str = "",
    to_status: str = "",
    state_version: int | None = None,
    driver_id: int | None = None,
    offer_id=None,
    reason: str = "",
    metadata: dict | None = None,
    occurred_at=None,
) -> TripEvent:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("audit.record() must run inside the transaction it describes")
    return TripEvent.objects.create(
        trip_id=trip.pk if hasattr(trip, "pk") else trip,
        event_type=event_type,
        state_version=state_version,
        from_status=from_status,
        to_status=to_status,
        actor_type=actor.type if actor else ActorType.SYSTEM,
        actor_id=actor.id if actor else None,
        driver_id=driver_id,
        offer_id=offer_id,
        reason=(reason or "")[:255],
        metadata=metadata or {},
        occurred_at=occurred_at or timezone.now(),
    )
