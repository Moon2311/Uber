"""Which events become push notifications, for whom, and for how long they matter.

Only important alerts are pushed. Everything else the user sees live in the app
over WebSockets (apps.realtime): searching, trip started, driver position, etc.
Templates carry no personal data (names, addresses): the app fetches details.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    body: str
    ttl_seconds: int
    collapse_key: str = ""  # per trip: a newer trip alert replaces an older one


TEMPLATES = {
    "driver_assigned": Alert("driver_assigned", "Driver on the way",
                             "Your driver is heading to your pickup.", 15 * 60, "trip"),
    "driver_arrived": Alert("driver_arrived", "Your driver has arrived",
                            "Meet your driver at the pickup point.", 15 * 60, "trip"),
    "driver_released": Alert("driver_released", "Finding you another driver",
                             "Your driver couldn't make it. We're matching you again.", 15 * 60,
                             "trip"),
    "no_driver": Alert("no_driver", "No drivers available",
                       "We couldn't find a driver. You have not been charged.", 30 * 60, "trip"),
    "trip_cancelled_driver": Alert("trip_cancelled_driver", "Trip cancelled",
                                   "The rider cancelled this trip.", 15 * 60, "trip"),
    "trip_cancelled_rider": Alert("trip_cancelled_rider", "Trip cancelled",
                                  "Your trip was cancelled.", 30 * 60, "trip"),
    "ride_offer": Alert("ride_offer", "New ride request",
                        "Open the app to accept before it expires.", 60, "offer"),
    "payment_paid": Alert("payment_paid", "Receipt", "Thanks for riding. Your receipt is ready.",
                          24 * 3600),
    "payment_failed": Alert("payment_failed", "Payment failed",
                            "We couldn't charge your card. Update your payment method.",
                            7 * 24 * 3600),
    "payment_refunded": Alert("payment_refunded", "Refund issued",
                              "Your refund is on its way.", 7 * 24 * 3600),
}  # fmt: skip


@dataclass(frozen=True)
class Planned:
    user_id: int
    alert: Alert
    data: dict


def _driver_user_id(driver_id) -> int | None:
    from apps.drivers.models import DriverProfile

    return DriverProfile.objects.filter(pk=driver_id).values_list("user_id", flat=True).first()


def plan(event) -> list[Planned]:
    """Event -> notifications to create (pure apart from small PostgreSQL lookups)."""
    p, t = event.payload, event.event_type
    out: list[Planned] = []
    if t == "TripStatusChanged":
        trip = {"trip_id": p["trip_id"]}
        to, frm = p["to_status"], p["from_status"]
        if to == "DRIVER_ASSIGNED":
            out.append(Planned(p["rider_id"], TEMPLATES["driver_assigned"], trip))
        elif to == "DRIVER_ARRIVING":
            out.append(Planned(p["rider_id"], TEMPLATES["driver_arrived"], trip))
        elif to == "SEARCHING" and frm in ("DRIVER_ASSIGNED", "DRIVER_ARRIVING"):
            out.append(Planned(p["rider_id"], TEMPLATES["driver_released"], trip))
        elif to == "CANCELLED":
            from apps.trips.models import Trip

            cancelled_by = (
                Trip.objects.filter(pk=p["trip_id"]).values_list("cancelled_by", flat=True).first()
            )
            if p.get("reason") == "no_driver_available":
                out.append(Planned(p["rider_id"], TEMPLATES["no_driver"], trip))
            elif cancelled_by != "RIDER":
                out.append(Planned(p["rider_id"], TEMPLATES["trip_cancelled_rider"], trip))
            if p.get("driver_id") and (user_id := _driver_user_id(p["driver_id"])):
                out.append(Planned(user_id, TEMPLATES["trip_cancelled_driver"], trip))
    elif t == "RideOfferCreated":
        if user_id := _driver_user_id(p["driver_id"]):
            out.append(Planned(user_id, TEMPLATES["ride_offer"],
                               {"trip_id": p["trip_id"], "offer_id": p["offer_id"]}))  # fmt: skip
    elif t == "PaymentStatusChanged":
        data = {"trip_id": p["trip_id"], "payment_id": p["payment_id"]}
        template = {"PAID": "payment_paid", "FAILED": "payment_failed",
                    "REFUNDED": "payment_refunded"}.get(p["to_status"])  # fmt: skip
        if template:
            out.append(Planned(p["rider_id"], TEMPLATES[template], data))
    return out
