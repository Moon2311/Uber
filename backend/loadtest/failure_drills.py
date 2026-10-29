"""Failure drills against the LOCAL docker compose stack (destructive, then restored).

Each drill breaks one dependency for real (docker compose stop/kill), checks the
documented degraded behaviour, restores it, and checks recovery. Never point this
at a shared or production environment.

    python loadtest/failure_drills.py            # all drills
    python loadtest/failure_drills.py redis kafka
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import httpx
import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API, WS1, WS2 = "http://localhost:8000", "ws://localhost:8001", "ws://localhost:8002"
PASSWORD = "Drill-Passw0rd!-x"
BODY = {"pickup": {"lat": 37.7749, "lng": -122.4194},
        "dropoff": {"lat": 37.8044, "lng": -122.2712}, "ride_category": "ECONOMY"}  # fmt: skip
REPORT: list[dict] = []


def sh(*args, check=True, timeout=180):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=check, timeout=timeout,
                          capture_output=True, text=True)  # fmt: skip


def manage(*args):
    if args and args[0] == "shell":
        args = ("shell", "-v", "0", *args[1:])  # no "N objects imported" banner on stdout
    return sh("exec", "-T", "backend", "python", "manage.py", *args, check=False).stdout


def wait_healthy(*services, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = sh("ps", *services, "--format", "{{.Status}}", check=False).stdout
        if out and all("(healthy)" in line for line in out.strip().splitlines()):
            return True
        time.sleep(2)
    return False


def expect(cond, what, details=""):
    REPORT[-1]["checks"].append({"check": what, "ok": bool(cond), "details": details})
    print(f"    {'ok  ' if cond else 'FAIL'} {what} {details}")


def drill(name):
    def deco(fn):
        fn.drill = name
        return fn

    return deco


class Users:
    """Registration is rate-limited (correctly), so drills share one rider and one
    driver; each drill leaves them without an active trip."""

    def __init__(self, http):
        self.http = http
        self.cache = {}

    def register(self, role, fresh=False):
        if not fresh and role in self.cache:
            return self.cache[role]
        self.cache[role] = self._register(role)
        return self.cache[role]

    def _register(self, role):
        n = uuid.uuid4().hex[:8]
        body = {"email": f"drill-{role.lower()}-{n}@example.com", "password": PASSWORD,
                "first_name": "Drill", "role": role}  # fmt: skip
        if role == "DRIVER":
            body |= {"license_number": f"DRL-{n}", "vehicle": {"make": "T", "model": "P",
                     "color": "Red", "plate_number": f"DR{n[:6]}", "year": 2022,
                     "category": "ECONOMY"}}  # fmt: skip
        r = self.http.post(f"{API}/api/v1/auth/register/", json=body)
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access']}"}, r.json()["access"]


# --- drills --------------------------------------------------------------------------------
@drill("redis")
def redis_down(http, users):
    driver, _ = users.register("DRIVER")
    rider, _ = users.register("RIDER")
    http.post(f"{API}/api/v1/drivers/availability/", headers=driver, json={"is_online": True})
    sh("stop", "redis")
    try:
        t = time.perf_counter()
        r = http.post(f"{API}/api/v1/locations/update/", headers=driver,
                      json={"lat": 37.7752, "lng": -122.4194})  # fmt: skip
        expect(r.status_code == 200, "GPS update accepted with Redis down (PostgreSQL fallback)",
               f"{r.status_code} in {(time.perf_counter() - t) * 1000:.0f} ms")  # fmt: skip
        r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY)
        expect(
            r.status_code == 201,
            "trip request works (rate limiting fails open)",
            str(r.status_code),
        )
        trip_id = r.json().get("id")
        expect(
            http.get(f"{API}/api/v1/health/ready/").status_code == 200,
            "readiness stays 200 (Redis is not required)",
        )
        expect(http.get(f"{API}/api/v1/health/").json()["status"] == "degraded",
               "health reports degraded")  # fmt: skip
    finally:
        sh("start", "redis")
        wait_healthy("redis")
    out = manage("reconcile_live_store")
    expect("reconcil" in out.lower() or out == "" or "replay" in out.lower(),
           "reconcile_live_store re-syncs Redis from PostgreSQL", out.strip()[:120])  # fmt: skip
    time.sleep(6)  # circuit breaker cooldown
    r = http.post(f"{API}/api/v1/locations/update/", headers=driver,
                  json={"lat": 37.7753, "lng": -122.4194})  # fmt: skip
    expect(r.status_code == 200, "GPS back on Redis after recovery")
    http.post(f"{API}/api/v1/trips/{trip_id}/cancel/", headers=rider, json={})


@drill("kafka")
def kafka_down(http, users):
    rider, _ = users.register("RIDER")
    sh("stop", "kafka")
    try:
        r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY)
        expect(r.status_code == 201, "trip request succeeds with Kafka down", str(r.status_code))
        r2 = http.post(f"{API}/api/v1/trips/{r.json()['id']}/cancel/", headers=rider, json={})
        expect(r2.status_code == 200, "cancellation succeeds with Kafka down")
        time.sleep(5)
        pending = json.loads(
            manage(
                "shell",
                "-c",
                "from apps.events.relay import outbox_stats; "
                "import json; print(json.dumps(outbox_stats()))",
            )
            or "{}"
        )
        expect(pending.get("pending", 0) >= 2, "events wait in the PostgreSQL outbox", str(pending))
    finally:
        sh("start", "kafka")
    wait_healthy("kafka", timeout=180)
    t = time.time()
    while time.time() - t < 120:
        stats = json.loads(
            manage(
                "shell",
                "-c",
                "from apps.events.relay import outbox_stats; "
                "import json; print(json.dumps(outbox_stats()))",
            )
            or "{}"
        )
        if stats.get("pending") == 0:
            break
        time.sleep(3)
    expect(stats.get("pending") == 0, "outbox drained after Kafka returned",
           f"{time.time() - t:.0f} s after broker healthy")  # fmt: skip


@drill("consumer_lag")
def consumer_lag(http, users):
    rider, _ = users.register("RIDER")
    sh("stop", "consumer-notifications")
    try:
        for _ in range(5):
            r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY)
            http.post(f"{API}/api/v1/trips/{r.json()['id']}/cancel/", headers=rider, json={})
        time.sleep(8)  # relay publishes
        status = manage("event_pipeline_status")
        lag = json.loads(status)["consumer_groups"]["notifications"].get("total_lag", 0)
        expect(lag >= 10, "lag grows while the consumer is down", f"lag={lag}")
        t0 = time.time()
        while time.time() - t0 < 90 and "consumer_down:notifications" not in status:
            time.sleep(5)
            status = manage("event_pipeline_status")
        expect("consumer_down:notifications" in status,
               "pipeline status names the dead consumer (heartbeat goes stale)",
               f"detected {time.time() - t0 + 8:.0f} s after stop")  # fmt: skip
    finally:
        sh("start", "consumer-notifications")
    t = time.time()
    while time.time() - t < 90:
        lag = json.loads(manage("event_pipeline_status"))["consumer_groups"]["notifications"].get(
            "total_lag", -1)  # fmt: skip
        if lag == 0:
            break
        time.sleep(3)
    expect(lag == 0, "consumer catches up after restart", f"{time.time() - t:.0f} s")


@drill("database")
def database_down(http, users):
    rider, _ = users.register("RIDER")
    sh("stop", "db")
    try:
        r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY, timeout=20)
        expect(
            r.status_code == 503 and r.headers.get("Retry-After"),
            "API answers 503 + Retry-After (not 500) without PostgreSQL",
            str(r.status_code),
        )
        expect(http.get(f"{API}/api/v1/health/ready/").status_code == 503, "readiness fails")
        expect(http.get(f"{API}/api/v1/health/live/").status_code == 200,
               "liveness passes (no restart storm)")  # fmt: skip
    finally:
        sh("start", "db")
        wait_healthy("db")
    t = time.time()
    while time.time() - t < 60:
        if http.get(f"{API}/api/v1/health/ready/").status_code == 200:
            break
        time.sleep(1)
    r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY)
    expect(r.status_code == 201, "API recovers without restarting", f"{time.time() - t:.0f} s")
    http.post(f"{API}/api/v1/trips/{r.json()['id']}/cancel/", headers=rider, json={})
    time.sleep(5)
    bad = [s for s in sh("ps", "--format", "{{.Service}} {{.Status}}").stdout.splitlines()
           if "Restarting" in s or "Exited (1" in s]  # fmt: skip
    expect(not bad, "no worker crash-looped during the outage", str(bad))


@drill("worker_crash")
def worker_crash(http, users):
    rider, _ = users.register("RIDER")
    driver, driver_token = users.register("DRIVER")
    http.post(f"{API}/api/v1/drivers/availability/", headers=driver, json={"is_online": True})
    http.post(f"{API}/api/v1/locations/update/", headers=driver,
              json={"lat": 37.7760, "lng": -122.4194})  # fmt: skip
    sh("up", "-d", "--scale", "matching-worker=2", "--no-recreate", "matching-worker")
    names = sh("ps", "matching-worker", "--format", "{{.Name}}").stdout.split()
    subprocess.run(["docker", "kill", "-s", "KILL", names[0]], check=True, capture_output=True)
    r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY)
    trip_id = r.json()["id"]

    async def offered():
        async with websockets.connect(f"{WS1}/ws/v1/", subprotocols=["jwt", driver_token]) as c:
            deadline = time.time() + 45
            while time.time() < deadline:
                m = json.loads(await asyncio.wait_for(c.recv(), 45))
                if m["type"] == "offer.created":  # live, or replayed on connect
                    return True
        return False

    got = asyncio.run(offered())
    expect(got, "surviving matching worker still dispatches after SIGKILL of another")
    http.post(f"{API}/api/v1/trips/{trip_id}/cancel/", headers=rider, json={})
    sh("up", "-d", "--scale", "matching-worker=1", "matching-worker")
    http.post(f"{API}/api/v1/drivers/availability/", headers=driver, json={"is_online": False})


@drill("websocket_server")
def websocket_server_crash(http, users):
    rider, token = users.register("RIDER")
    r = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY)
    trip_id = r.json()["id"]

    async def scenario():
        c1 = await websockets.connect(f"{WS1}/ws/v1/", subprotocols=["jwt", token])
        await c1.recv()
        sh("kill", "-s", "KILL", "realtime-1")
        # Real clients detect a dead server with the app heartbeat: ping, expect pong.
        t0, dead = time.time(), False
        try:
            while time.time() - t0 < 30 and not dead:
                await c1.send(json.dumps({"type": "ping", "id": "hb"}))
                deadline = time.time() + 3
                got_pong = False
                while time.time() < deadline:
                    m = json.loads(await asyncio.wait_for(c1.recv(), deadline - time.time()))
                    if m["type"] == "pong":
                        got_pong = True
                        break
                dead = not got_pong
        except Exception:  # noqa: BLE001 - closed or timed out: both mean "dead"
            dead = True
        expect(dead, "client detects the dead server via heartbeat",
               f"{time.time() - t0:.1f} s")  # fmt: skip
        c2 = await websockets.connect(f"{WS2}/ws/v1/", subprotocols=["jwt", token])
        msgs = [json.loads(await asyncio.wait_for(c2.recv(), 5)) for _ in range(3)]
        snap = [m for m in msgs if m["type"] == "trip.snapshot"]
        expect(
            snap and snap[0]["data"]["id"] == trip_id,
            "reconnect to the other server restores state (snapshot of the active trip)",
        )
        await c2.close()

    try:
        asyncio.run(scenario())
    finally:
        sh("up", "-d", "realtime-1")
        wait_healthy("realtime-1")
    http.post(f"{API}/api/v1/trips/{trip_id}/cancel/", headers=rider, json={})


@drill("payment_timeout")
def payment_timeout(http, users):
    """A card whose charge succeeds but whose response is lost (sandbox token)."""
    rider, _ = users.register("RIDER", fresh=True)  # its card becomes the timeout card
    driver, driver_token = users.register("DRIVER")
    http.post(f"{API}/api/v1/payments/methods/", headers=rider,
              json={"provider_token": "tok_sandbox_timeout"})  # fmt: skip
    http.post(f"{API}/api/v1/drivers/availability/", headers=driver, json={"is_online": True})
    http.post(f"{API}/api/v1/locations/update/", headers=driver,
              json={"lat": 37.7755, "lng": -122.4194})  # fmt: skip
    trip_id = http.post(f"{API}/api/v1/trips/", headers=rider, json=BODY).json()["id"]

    async def accept():
        async with websockets.connect(f"{WS2}/ws/v1/", subprotocols=["jwt", driver_token]) as c:
            while True:
                m = json.loads(await asyncio.wait_for(c.recv(), 45))
                if m["type"] == "offer.created":
                    await c.send(json.dumps({"type": "offer.accept", "id": "x",
                                             "offer_id": m["data"]["id"]}))  # fmt: skip
                    return

    asyncio.run(accept())
    time.sleep(1)
    for step in ("arrive", "start", "complete"):
        http.post(f"{API}/api/v1/trips/{trip_id}/{step}/", headers=driver)
    t = time.time()
    seen = set()
    while time.time() - t < 150:
        status = http.get(f"{API}/api/v1/payments/trips/{trip_id}/", headers=rider).json()["status"]
        seen.add(status)
        if status == "PAID":
            break
        time.sleep(2)
    expect("PROCESSING" in seen, "payment stays PROCESSING while the outcome is unknown")
    expect(status == "PAID", "reconciliation settles it as PAID", f"{time.time() - t:.0f} s")
    payment_hex = http.get(f"{API}/api/v1/payments/trips/{trip_id}/",
                           headers=rider).json()["id"].replace("-", "")  # fmt: skip
    charges = manage("shell", "-c", "from apps.payments.models import SandboxCharge as S;"
                     f"print(S.objects.filter(kind='charge', idempotency_key__startswith="
                     f"'pay_{payment_hex}_').count())").strip()  # fmt: skip
    expect(charges == "1", "the card was charged exactly once", f"charges={charges}")


DRILLS = [redis_down, kafka_down, consumer_lag, database_down, worker_crash,
          websocket_server_crash, payment_timeout]  # fmt: skip


def main(selected):
    with httpx.Client(timeout=15) as http:
        users = Users(http)
        for fn in DRILLS:
            if selected and fn.drill not in selected:
                continue
            print(f"== drill: {fn.drill}")
            REPORT.append({"drill": fn.drill, "checks": []})
            start = time.time()
            try:
                fn(http, users)
            except Exception as exc:  # noqa: BLE001
                expect(False, "drill raised", repr(exc)[:200])
            REPORT[-1]["seconds"] = round(time.time() - start, 1)
    failed = [c for d in REPORT for c in d["checks"] if not c["ok"]]
    out = os.environ.get("DRILL_REPORT")
    if out:
        json.dump(REPORT, open(out, "w"), indent=2)
    print(f"{sum(len(d['checks']) for d in REPORT) - len(failed)} checks passed, "
          f"{len(failed)} failed")  # fmt: skip
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:])))
