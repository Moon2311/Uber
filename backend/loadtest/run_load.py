"""Load generator for the ride-hailing stack (HTTP + WebSockets).

Open-loop: requests are scheduled at a fixed arrival rate, independent of how fast
the server answers, so a slow server shows up as latency and errors instead of
silently reducing the offered load (no coordinated omission).

Scenarios
  gps    drivers POST /locations/update at a target total rate (stepped levels)
  ws     hold N WebSocket connections with heartbeats; measure connect latency
  rides  riders request trips at a fixed rate; WebSocket-connected drivers
         auto-accept offers; trips are driven to completion so drivers free up.
         Measures request->offer (matching) and request->assigned latencies.
  mixed  gps + ws + rides at the same time

Setup (users + tokens, development stack only):
  docker compose exec backend python manage.py seed_load_users --drivers 300 --riders 150
  docker compose cp backend:/tmp/load-users.json /tmp/load-users.json

Results are measurements of THIS deployment on THIS machine (generator included);
they are not capacity claims for other hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx
import websockets


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1)], 1)


@dataclass
class Stats:
    name: str
    latencies: list[float] = field(default_factory=list)
    codes: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    started: float = field(default_factory=time.perf_counter)
    finished: float | None = None

    def record(self, ms, code):
        self.latencies.append(ms)
        self.codes[code] += 1

    def summary(self, offered_rps=None) -> dict:
        duration = (self.finished or time.perf_counter()) - self.started
        total = sum(self.codes.values()) + sum(self.errors.values())
        ok = sum(n for c, n in self.codes.items() if 200 <= c < 300)
        return {
            "scenario": self.name, "offered_rps": offered_rps, "duration_s": round(duration, 1),
            "requests": total, "achieved_rps": round(total / duration, 1) if duration else 0,
            "success_ratio": round(ok / total, 4) if total else None,
            "p50_ms": pct(self.latencies, 50), "p95_ms": pct(self.latencies, 95),
            "p99_ms": pct(self.latencies, 99), "max_ms": pct(self.latencies, 100),
            "status_codes": dict(self.codes), "errors": dict(self.errors),
        }  # fmt: skip


async def open_loop(rate, duration, make_call):
    """Fire make_call() at `rate` per second for `duration` seconds (Poisson arrivals)."""
    tasks, deadline = [], time.perf_counter() + duration
    while time.perf_counter() < deadline:
        tasks.append(asyncio.create_task(make_call()))
        await asyncio.sleep(random.expovariate(rate))
    await asyncio.gather(*tasks, return_exceptions=True)


async def timed_request(client, stats, method, url, **kw):
    start = time.perf_counter()
    try:
        r = await client.request(method, url, **kw)
        stats.record((time.perf_counter() - start) * 1000, r.status_code)
        return r
    except Exception as exc:  # noqa: BLE001 - every failure is a data point
        stats.errors[type(exc).__name__] += 1
        return None


# --- scenarios ---------------------------------------------------------------------------------
async def gps(api, users, rates, step_seconds):
    drivers = users["drivers"]
    lat0, lng0 = users["center"]
    positions = {d["id"]: [lat0 + random.uniform(-0.03, 0.03), lng0 + random.uniform(-0.03, 0.03)]
                 for d in drivers}  # fmt: skip
    results = []
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=400)
    async with httpx.AsyncClient(base_url=api, timeout=10, limits=limits) as client:
        for rate in rates:
            stats = Stats(f"gps@{rate}rps")

            async def call(stats=stats):  # bind this step's stats explicitly
                d = random.choice(drivers)
                p = positions[d["id"]]
                p[0] += random.uniform(-0.0004, 0.0004)
                p[1] += random.uniform(-0.0004, 0.0004)
                await timed_request(client, stats, "POST", "/api/v1/locations/update/",
                                    json={"lat": round(p[0], 6), "lng": round(p[1], 6),
                                          "accuracy_m": 8},
                                    headers={"Authorization": f"Bearer {d['token']}"})  # fmt: skip

            await open_loop(rate, step_seconds, call)
            stats.finished = time.perf_counter()
            results.append(stats.summary(offered_rps=rate))
            print(json.dumps(results[-1]))
    return results


async def ws_hold(ws_urls, users, count, hold_seconds):
    tokens = [u["token"] for u in users["drivers"] + users["riders"]]
    connect_ms, errors, open_conns = [], Counter(), []

    async def one(i):
        url = ws_urls[i % len(ws_urls)]
        start = time.perf_counter()
        try:
            conn = await websockets.connect(
                f"{url}/ws/v1/",
                subprotocols=["jwt", tokens[i % len(tokens)]],
                open_timeout=15,
                ping_interval=None,
            )
            await asyncio.wait_for(conn.recv(), 15)  # welcome
            connect_ms.append((time.perf_counter() - start) * 1000)
            open_conns.append(conn)
        except Exception as exc:  # noqa: BLE001
            errors[type(exc).__name__] += 1

    start = time.perf_counter()
    for batch in range(0, count, 100):  # ramp in batches (realistic reconnect storms are worse)
        await asyncio.gather(*(one(i) for i in range(batch, min(count, batch + 100))))
    ramp_s = time.perf_counter() - start
    alive_after, pong_ms = 0, []
    deadline = time.perf_counter() + hold_seconds

    async def heartbeat(conn):
        nonlocal alive_after
        try:
            while time.perf_counter() < deadline:
                t = time.perf_counter()
                await conn.send(json.dumps({"type": "ping", "id": "p"}))
                while True:
                    msg = json.loads(await asyncio.wait_for(conn.recv(), 10))
                    if msg.get("type") == "pong":
                        pong_ms.append((time.perf_counter() - t) * 1000)
                        break
                await asyncio.sleep(min(20, max(0, deadline - time.perf_counter())))
            alive_after += 1
        except Exception as exc:  # noqa: BLE001
            errors[f"hold:{type(exc).__name__}"] += 1

    await asyncio.gather(*(heartbeat(c) for c in open_conns))
    await asyncio.gather(*(c.close() for c in open_conns), return_exceptions=True)
    result = {
        "scenario": f"ws@{count}", "requested": count, "connected": len(connect_ms),
        "alive_after_hold": alive_after, "hold_s": hold_seconds, "ramp_s": round(ramp_s, 1),
        "connect_p50_ms": pct(connect_ms, 50), "connect_p95_ms": pct(connect_ms, 95),
        "connect_p99_ms": pct(connect_ms, 99), "pong_p95_ms": pct(pong_ms, 95),
        "errors": dict(errors),
    }  # fmt: skip
    print(json.dumps(result))
    return result


async def rides(api, ws_urls, users, rate, duration, n_drivers):
    """Riders request at `rate`/s; drivers accept offers over WebSocket and complete trips."""
    lat0, lng0 = users["center"]
    drivers = users["drivers"][:n_drivers]
    riders = list(users["riders"])
    random.shuffle(riders)
    free_riders = asyncio.Queue()
    for r in riders:
        free_riders.put_nowait(r)
    req, offer_lat, assign_lat = Stats(f"ride_request@{rate}/s"), [], []
    outcome = Counter()
    pending: dict[str, dict] = {}  # trip_id -> timing
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    stop = asyncio.Event()

    async with httpx.AsyncClient(base_url=api, timeout=15, limits=limits) as client:
        # Drivers: fresh GPS near the center + WebSocket listener that accepts offers.
        for d in drivers:
            await client.post(
                "/api/v1/locations/update/",
                headers={"Authorization": f"Bearer {d['token']}"},
                json={
                    "lat": lat0 + random.uniform(-0.01, 0.01),
                    "lng": lng0 + random.uniform(-0.01, 0.01),
                },
            )

        async def gps_keepalive(d):
            while not stop.is_set():
                await asyncio.sleep(30)
                await client.post("/api/v1/locations/update/",
                                  headers={"Authorization": f"Bearer {d['token']}"},
                                  json={"lat": lat0 + random.uniform(-0.01, 0.01),
                                        "lng": lng0 + random.uniform(-0.01, 0.01)})  # fmt: skip

        async def driver_loop(i, d):
            auth = {"Authorization": f"Bearer {d['token']}"}
            try:
                async with websockets.connect(
                    f"{ws_urls[i % len(ws_urls)]}/ws/v1/", subprotocols=["jwt", d["token"]]
                ) as conn:
                    last_ping = time.perf_counter()
                    while not stop.is_set():
                        if time.perf_counter() - last_ping > 20:  # app-level heartbeat
                            await conn.send(json.dumps({"type": "ping", "id": "hb"}))
                            last_ping = time.perf_counter()
                        try:
                            msg = json.loads(await asyncio.wait_for(conn.recv(), 1))
                        except (TimeoutError, asyncio.TimeoutError):
                            continue
                        if msg.get("type") != "offer.created":
                            continue
                        trip_id = (
                            msg["data"]["trip"]["id"]
                            if "trip" in msg["data"]
                            else msg["data"].get("trip_id")
                        )
                        t = pending.get(str(trip_id))
                        if t and "offer" not in t:
                            t["offer"] = time.perf_counter()
                            offer_lat.append((t["offer"] - t["start"]) * 1000)
                        await conn.send(json.dumps({"type": "offer.accept", "id": "a",
                                                    "offer_id": msg["data"]["id"]}))  # fmt: skip
                        res = await wait_type(conn, "offer.result", 10)
                        accepted = res and (res.get("ok") or "ACCEPTED" in json.dumps(res))
                        if not accepted:
                            outcome["accept_rejected"] += 1
                            continue
                        if t:
                            assign_lat.append((time.perf_counter() - t["start"]) * 1000)
                        for step in ("arrive", "start", "complete"):
                            await client.post(f"/api/v1/trips/{trip_id}/{step}/", headers=auth)
                        outcome["completed"] += 1
                        if t:
                            free_riders.put_nowait(t["rider"])
            except Exception as exc:  # noqa: BLE001
                outcome[f"driver_ws:{type(exc).__name__}"] += 1

        driver_tasks = [asyncio.create_task(driver_loop(i, d)) for i, d in enumerate(drivers)]
        keepalives = [asyncio.create_task(gps_keepalive(d)) for d in drivers]
        await asyncio.sleep(3)  # sockets connected

        async def request():
            try:
                rider = free_riders.get_nowait()
            except asyncio.QueueEmpty:
                outcome["no_free_rider"] += 1
                return
            start = time.perf_counter()
            r = await timed_request(client, req, "POST", "/api/v1/trips/",
                                    headers={"Authorization": f"Bearer {rider['token']}"},
                                    json={"pickup": {"lat": lat0, "lng": lng0},
                                          "dropoff": {"lat": lat0 + 0.03, "lng": lng0 + 0.03},
                                          "ride_category": "ECONOMY"})  # fmt: skip
            if r is not None and r.status_code == 201:
                pending[r.json()["id"]] = {"start": start, "rider": rider}
            else:
                outcome[f"request_{getattr(r, 'status_code', 'error')}"] += 1
                free_riders.put_nowait(rider)

        await open_loop(rate, duration, request)
        req.finished = time.perf_counter()
        await asyncio.sleep(20)  # let in-flight matches finish
        stop.set()
        await asyncio.gather(*driver_tasks, *keepalives, return_exceptions=True)
    result = {
        **req.summary(offered_rps=rate),
        "drivers": len(drivers),
        "trips_created": len(pending),
        "offers_seen": len(offer_lat),
        "request_to_offer_p50_ms": pct(offer_lat, 50),
        "request_to_offer_p95_ms": pct(offer_lat, 95),
        "request_to_assigned_p50_ms": pct(assign_lat, 50),
        "request_to_assigned_p95_ms": pct(assign_lat, 95),
        "outcomes": dict(outcome),
    }
    print(json.dumps(result))
    return result


async def wait_type(conn, type_, timeout):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            msg = json.loads(await asyncio.wait_for(conn.recv(), deadline - time.perf_counter()))
        except (TimeoutError, asyncio.TimeoutError):  # distinct classes before Python 3.11
            return None
        if msg.get("type") == type_:
            return msg
    return None


async def main(args):
    users = json.load(open(args.users))
    ws_urls = args.ws.split(",")
    out = []
    if args.scenario in ("gps",):
        out += await gps(args.api, users, [int(r) for r in args.rates.split(",")], args.step)
    if args.scenario == "ws":
        for n in [int(x) for x in args.connections.split(",")]:
            out.append(await ws_hold(ws_urls, users, n, args.hold))
    if args.scenario == "rides":
        out.append(await rides(args.api, ws_urls, users, args.ride_rate, args.step, args.drivers))
    if args.scenario == "mixed":
        results = await asyncio.gather(
            gps(args.api, users, [int(args.rates.split(",")[0])], args.step),
            ws_hold(ws_urls, users, int(args.connections.split(",")[0]), args.step),
            rides(args.api, ws_urls, users, args.ride_rate, args.step, args.drivers),
        )
        out += [*results[0], results[1], results[2]]
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"args": vars(args), "results": out}, fh, indent=2)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["gps", "ws", "rides", "mixed"])
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--ws", default="ws://localhost:8001,ws://localhost:8002")
    p.add_argument("--users", default="/tmp/load-users.json")  # noqa: S108
    p.add_argument("--rates", default="50,100,200,400", help="gps: total req/s per step")
    p.add_argument("--step", type=float, default=30, help="seconds per step / scenario")
    p.add_argument("--connections", default="500,1000,2000")
    p.add_argument("--hold", type=float, default=45)
    p.add_argument("--ride-rate", type=float, default=2)
    p.add_argument("--drivers", type=int, default=100)
    p.add_argument("--out")
    sys.exit(0 if asyncio.run(main(p.parse_args())) else 1)
