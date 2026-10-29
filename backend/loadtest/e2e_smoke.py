"""End-to-end smoke test against a RUNNING stack (docker compose up).

Exercises the whole ride the way the apps would, over HTTP and WebSockets, through
every real worker (matching, outbox relay, Kafka consumers, payments, notifications):

  register -> card -> driver online + GPS -> quote -> trip (idempotent) ->
  offer over WS -> accept over WS -> milestones -> card charged -> push queued

and checks the security properties on the way (ownership, duplicate requests,
role checks, token revocation). Exits non-zero on the first failed expectation.

    python loadtest/e2e_smoke.py --api http://localhost:8000 --ws ws://localhost:8001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid

import httpx
import websockets

PICKUP = {"lat": 37.7749, "lng": -122.4194}
DROPOFF = {"lat": 37.8044, "lng": -122.2712}
PASSWORD = "Smoke-Test-Passw0rd!"
STEPS: list[tuple[str, float]] = []


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


class Step:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = time.perf_counter()

    def __exit__(self, *exc):
        if exc[0] is None:
            ms = (time.perf_counter() - self.start) * 1000
            STEPS.append((self.name, ms))
            print(f"  ok  {self.name:55} {ms:8.1f} ms")


async def register(http, role, n):
    body = {"email": f"smoke-{role.lower()}-{n}@example.com", "password": PASSWORD,
            "first_name": "Smoke", "role": role}  # fmt: skip
    if role == "DRIVER":
        body |= {"license_number": f"SMK-{n}", "vehicle": {"make": "Toyota", "model": "Prius",
                 "color": "Blue", "plate_number": f"SMK{n[:6]}", "year": 2021,
                 "category": "ECONOMY"}}  # fmt: skip
    r = await http.post("/api/v1/auth/register/", json=body)
    check(r.status_code == 201, f"register {role}: {r.status_code} {r.text[:200]}")
    data = r.json()
    return data["access"], data["refresh"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def ws_connect(ws_url, token):
    conn = await websockets.connect(f"{ws_url}/ws/v1/", subprotocols=["jwt", token],
                                    open_timeout=5)  # fmt: skip
    return conn


async def wait_for(conn, type_, timeout=30.0, pred=lambda m: True):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(conn.recv(), timeout=max(0.1, deadline - time.monotonic()))
        msg = json.loads(raw)
        if msg.get("type") == type_ and pred(msg):
            return msg
    raise TimeoutError(type_)


async def poll(fn, timeout=30.0, every=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := await fn():
            return result
        await asyncio.sleep(every)
    return None


async def main(api, ws_url):
    n = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=api, timeout=10) as http:
        print(f"e2e smoke against {api} / {ws_url}")
        with Step("health: live + ready"):
            check((await http.get("/api/v1/health/live/")).status_code == 200, "live")
            check((await http.get("/api/v1/health/ready/")).status_code == 200, "ready")

        with Step("register rider, driver, second rider"):
            rider, rider_refresh = await register(http, "RIDER", n)
            driver, _ = await register(http, "DRIVER", n)
            stranger, _ = await register(http, "RIDER", n + "x")

        with Step("rider attaches a card (token only)"):
            r = await http.post("/api/v1/payments/methods/", headers=auth(rider),
                                json={"provider_token": "tok_sandbox_visa"})  # fmt: skip
            check(r.status_code == 201 and "tok_" not in r.text, f"card: {r.text[:200]}")
            r = await http.post("/api/v1/payments/methods/", headers=auth(rider),
                                json={"provider_token": "4242424242424242"})  # fmt: skip
            check(r.status_code == 400, "raw card number must be refused")
            r = await http.post("/api/v1/notifications/devices/", headers=auth(rider),
                                json={"platform": "IOS", "token": f"dev_ok_{n}"})  # fmt: skip
            check(r.status_code == 201, "device registration")

        with Step("driver online + GPS fix"):
            r = await http.post("/api/v1/drivers/availability/", headers=auth(driver),
                                json={"is_online": True})  # fmt: skip
            check(r.status_code == 200, f"online: {r.text[:200]}")
            r = await http.post(
                "/api/v1/locations/update/",
                headers=auth(driver),
                json={"lat": PICKUP["lat"] + 0.003, "lng": PICKUP["lng"]},
            )
            check(r.status_code == 200 and r.json()["accepted"], f"gps: {r.text[:200]}")

        rider_ws = await ws_connect(ws_url, rider)
        driver_ws = await ws_connect(ws_url, driver)
        await wait_for(rider_ws, "synced")
        await wait_for(driver_ws, "synced")

        body = {"pickup": PICKUP, "dropoff": DROPOFF, "ride_category": "ECONOMY"}
        with Step("quote"):
            q = (
                await http.post("/api/v1/pricing/estimate/", headers=auth(rider), json=body)
            ).json()
            check("quote_id" in q, f"quote: {q}")

        key = {"Idempotency-Key": f"smoke-{n}", "X-Request-ID": f"smoke-req-{n}"}
        with Step("request trip (+ retry with same Idempotency-Key)"):
            r1 = await http.post("/api/v1/trips/", headers=auth(rider) | key,
                                 json=body | {"quote_id": q["quote_id"]})  # fmt: skip
            check(r1.status_code == 201, f"trip: {r1.status_code} {r1.text[:300]}")
            check(r1.headers.get("X-Request-ID") == f"smoke-req-{n}", "request id echoed")
            r2 = await http.post("/api/v1/trips/", headers=auth(rider) | key,
                                 json=body | {"quote_id": q["quote_id"]})  # fmt: skip
            check(
                r2.status_code == 201
                and r2.json()["id"] == r1.json()["id"]
                and r2.headers.get("Idempotent-Replayed") == "true",
                "idempotent replay",
            )
            r3 = await http.post("/api/v1/trips/", headers=auth(rider), json=body)
            check(r3.status_code == 409, "second active trip must be refused")
        trip = r1.json()
        trip_id = trip["id"]

        with Step("unauthorized access is refused"):
            r = await http.get(f"/api/v1/trips/{trip_id}/", headers=auth(stranger))
            check(r.status_code == 404, "stranger must not see the trip")
            r = await http.post(f"/api/v1/trips/{trip_id}/arrive/", headers=auth(driver))
            check(r.status_code == 404, "unassigned driver cannot move the trip")
            r = await http.post(f"/api/v1/trips/{trip_id}/cancel/", headers=auth(driver))
            check(r.status_code == 403, "drivers cannot use rider endpoints")
            r = await http.get(f"/api/v1/trips/{trip_id}/events/", headers=auth(rider))
            check(r.status_code == 403, "audit trail is staff-only")

        with Step("matching: offer reaches the driver over WebSocket"):
            offer = await wait_for(driver_ws, "offer.created", timeout=30)
        with Step("driver accepts over WebSocket; rider is told"):
            await driver_ws.send(json.dumps({"type": "offer.accept", "id": "a1",
                                             "offer_id": offer["data"]["id"]}))  # fmt: skip
            result = await wait_for(driver_ws, "offer.result")
            check(result.get("ok") or result.get("status") == "ACCEPTED"
                  or result.get("offer_status") == "ACCEPTED", f"accept: {result}")  # fmt: skip
            await wait_for(rider_ws, "trip.status",
                           pred=lambda m: m["data"]["status"] == "DRIVER_ASSIGNED")  # fmt: skip

        with Step("arrive / start / complete (REST, assigned driver)"):
            for step in ("arrive", "start", "complete"):
                r = await http.post(f"/api/v1/trips/{trip_id}/{step}/", headers=auth(driver))
                check(r.status_code == 200, f"{step}: {r.status_code} {r.text[:200]}")
            check(r.json()["status"] == "COMPLETED", "completed")

        with Step("payments worker charges the card (PAID) + live WS update"):

            async def paid():
                p = await http.get(f"/api/v1/payments/trips/{trip_id}/", headers=auth(rider))
                return p.json() if p.status_code == 200 and p.json()["status"] == "PAID" else None

            payment = await poll(paid, timeout=30)
            check(payment and payment["amount"] == trip["estimated_fare"], f"payment: {payment}")
            await wait_for(rider_ws, "payment.status", pred=lambda m: m["data"]["status"] == "PAID")

        with Step("notifications consumer queued push alerts (via Kafka)"):

            async def kinds():
                r = await http.get("/api/v1/notifications/", headers=auth(rider))
                found = {x["kind"] for x in r.json()["results"]}
                return found if {"driver_assigned", "payment_paid"} <= found else None

            check(await poll(kinds, timeout=45), "driver_assigned + payment_paid notifications")

        with Step("logout revokes the refresh token"):
            r = await http.post("/api/v1/auth/logout/", headers=auth(rider),
                                json={"refresh": rider_refresh})  # fmt: skip
            check(r.status_code == 205, f"logout {r.status_code}")
            r = await http.post("/api/v1/auth/refresh/", json={"refresh": rider_refresh})
            check(r.status_code == 401, "revoked refresh token must be refused")

        await rider_ws.close()
        await driver_ws.close()
    total = sum(ms for _, ms in STEPS)
    print(f"PASS: {len(STEPS)} steps in {total / 1000:.1f} s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--ws", default="ws://localhost:8001")
    args = parser.parse_args()
    asyncio.run(main(args.api, args.ws))
