#!/usr/bin/env python3
"""Boot the whole stack for real and prove data flows end to end.

Unlike the pytest suites, which import the app in-process, this starts the
actual server processes and talks to them over real sockets:

    amqtt broker (:PORT) ── uvicorn (:PORT) ── simulator ── HTTP + WebSocket

It then asserts every REST endpoint's response shape, opens a live WebSocket,
and checks that the flashover scenario produces a FIRE alert.

Usage::

    python scripts/smoke_test.py                 # normal scenario
    python scripts/smoke_test.py --scenario flashover
    python scripts/smoke_test.py --keep-running  # leave it up to poke at

Exit code 0 means the stack works. Anything else means it does not.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36m··\033[0m"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{PASS if condition else FAIL}] {label}{f'  {detail}' if detail else ''}")
    if not condition:
        _failures.append(label)
    return condition


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode())


def wait_for_http(url: str, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = get(url, timeout=2.0)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

BROKER_SCRIPT = """
import asyncio, sys
from amqtt.broker import Broker

async def main(port):
    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
        },
    })
    await broker.start()
    await asyncio.Event().wait()

asyncio.run(main(int(sys.argv[1])))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=("normal", "smoldering", "flashover"), default="flashover"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=60.0,
        help=(
            "maximum seconds to wait for the scenario to reach its expected "
            "state. The run stops as soon as that happens (flashover typically "
            "alarms in ~12 s), so a generous ceiling costs nothing and avoids "
            "cutting the fire off mid-ramp."
        ),
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="leave the stack up after the checks pass, so you can browse it",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=0,
        help=(
            "pin the backend port instead of picking a free one. Use --api-port "
            "8000 if you want to run the dashboard against it: the Vite dev "
            "proxy targets localhost:8000."
        ),
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=0, help="pin the broker port (0 = pick free)"
    )
    args = parser.parse_args()

    mqtt_port = args.mqtt_port or free_port()
    api_port = args.api_port or free_port()
    procs: list[subprocess.Popen] = []

    # The smoke database lives in the system temp dir, not the repo: it is
    # throwaway state, and on network/virtualised mounts SQLite's WAL locking
    # can stall outright. Keeping it local also means the test never disturbs
    # a real fireprotect.db.
    workdir = Path(tempfile.mkdtemp(prefix="fireprotect-smoke-"))
    db_path = workdir / "smoke.db"

    env = os.environ.copy()
    env.update(
        {
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": str(mqtt_port),
            "DATABASE_URL": f"sqlite:///{db_path}",
            "MODEL_DIR": str(REPO_ROOT / "ml"),
            "THINGSPEAK_ENABLED": "false",
            "THINGSPEAK_BUFFER_PATH": str(workdir / "thingspeak_buffer.jsonl"),
            "PYTHONPATH": str(REPO_ROOT / "backend"),
            "PYTHONUNBUFFERED": "1",
        }
    )

    print(f"\n{INFO} FireProtect stack smoke test")
    print(f"{INFO} broker :{mqtt_port}   api :{api_port}   scenario {args.scenario}\n")

    try:
        # --- 1. broker ----------------------------------------------------
        print("1. MQTT broker")
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", BROKER_SCRIPT, str(mqtt_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=REPO_ROOT,
            )
        )
        check("broker accepts TCP connections", wait_for_port(mqtt_port))

        # --- 2. backend ---------------------------------------------------
        print("\n2. Backend API")
        procs.append(
            subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "app.main:app",
                    "--host", "127.0.0.1", "--port", str(api_port),
                    "--log-level", "warning",
                ],
                cwd=REPO_ROOT / "backend",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        base = f"http://127.0.0.1:{api_port}"
        if not check("API responds on /api/health", wait_for_http(f"{base}/api/health")):
            return 1

        _, health = get(f"{base}/api/health")
        check("database reachable", health["database"] is True)
        check("random forest loaded", health["model_loaded"] is True)
        check(
            "MQTT connected", health["mqtt_connected"] is True,
            f"(v{health['version']})",
        )

        # --- 3. simulator -------------------------------------------------
        print(f"\n3. Simulator ({args.scenario}) publishing over MQTT")
        procs.append(
            subprocess.Popen(
                [
                    sys.executable, "simulator/virtual_device.py",
                    "--host", "127.0.0.1", "--port", str(mqtt_port),
                    "--scenario", args.scenario,
                    "--device-id", f"smoke-{args.scenario}",
                    "--location", "Smoke Test",
                    "--interval", "0.25", "--sim-step", "2.0", "--quiet",
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

        # Wait for the scenario's *expected outcome*, not a fixed wall-clock
        # budget. An earlier version used a flat timeout and intermittently
        # cut the flashover off at WARNING before it had ramped to FIRE.
        deadline = time.monotonic() + args.seconds
        readings = 0
        reached = False
        while time.monotonic() < deadline:
            try:
                _, rows = get(f"{base}/api/readings?limit=1000")
                readings = len(rows)
                if args.scenario == "flashover":
                    _, alerts = get(f"{base}/api/alerts")
                    reached = any(a["severity"] == "FIRE" for a in alerts)
                elif args.scenario == "smoldering":
                    _, alerts = get(f"{base}/api/alerts")
                    reached = bool(alerts)
                else:
                    # `normal` has no positive outcome to wait for; collect a
                    # representative sample and rely on the absence of alerts.
                    reached = readings >= 30
                if reached:
                    break
            except OSError:
                pass
            time.sleep(0.6)

        if not reached and args.scenario != "normal":
            print(
                f"  {INFO} scenario did not reach its expected state within "
                f"{args.seconds:.0f}s ({readings} readings) - raise --seconds"
            )

        check(
            "telemetry reached the database over MQTT",
            readings > 0,
            f"{readings} readings",
        )

        # --- 4. REST contract ---------------------------------------------
        print("\n4. REST endpoints")
        _, devices = get(f"{base}/api/devices")
        check("GET /api/devices returns the device", len(devices) >= 1)
        if devices:
            device_id = devices[0]["id"]
            check(
                "device fields present",
                {"id", "location", "online", "last_seen"} <= set(devices[0]),
            )
            _, detail = get(f"{base}/api/devices/{device_id}")
            check(
                "GET /api/devices/{id} has latest_reading",
                detail["latest_reading"] is not None,
            )
            _, dev_readings = get(f"{base}/api/devices/{device_id}/readings?limit=5")
            check("GET /api/devices/{id}/readings", len(dev_readings) > 0)

        _, rows = get(f"{base}/api/readings?limit=5")
        expected = {
            "id", "device_id", "recorded_at", "temperature_c", "humidity_pct",
            "smoke_ppm", "air_quality_ppm", "flame_analog_volts", "flame_detected",
            "temp_rate_c_per_min", "smoke_rate_ppm_per_min", "heat_index_c",
            "device_status", "server_status", "fire_probability", "risk_score",
        }
        check("reading shape complete", bool(rows) and expected <= set(rows[0]))
        check(
            "server-side model ran",
            bool(rows) and rows[0]["server_status"] in {"SAFE", "WARNING", "FIRE"},
            f"status={rows[0]['server_status']} p={rows[0]['fire_probability']:.3f}"
            if rows else "",
        )

        _, stats = get(f"{base}/api/stats")
        check("GET /api/stats", stats["total_readings"] > 0,
              f"{stats['total_readings']} readings, status {stats['current_status']}")

        _, thresholds = get(f"{base}/api/thresholds")
        check("GET /api/thresholds seeded", len(thresholds) == 7)

        request = urllib.request.Request(
            f"{base}/api/predict",
            data=json.dumps({
                "temperature_c": 95.0, "humidity_pct": 15.0, "smoke_ppm": 9500.0,
                "air_quality_ppm": 8000.0, "flame_analog_volts": 3.0,
                "flame_detected": 1, "temp_rate_c_per_min": 40.0,
                "smoke_rate_ppm_per_min": 1200.0,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            prediction = json.loads(response.read().decode())
        check("POST /api/predict classifies fire", prediction["status"] == "FIRE",
              f"p(fire)={prediction['fire_probability']:.3f}")

        # --- 5. alerting --------------------------------------------------
        print("\n5. Alerting")
        _, alerts = get(f"{base}/api/alerts")
        if args.scenario == "flashover":
            fire = [a for a in alerts if a["severity"] == "FIRE"]
            check("flashover raised a FIRE alert", bool(fire),
                  fire[0]["message"][:70] + "…" if fire else "none raised")
        elif args.scenario == "normal":
            check("normal raised no FIRE alert",
                  not [a for a in alerts if a["severity"] == "FIRE"])
        else:
            check("smoldering raised an alert", bool(alerts))

        # --- 6. websocket -------------------------------------------------
        print("\n6. WebSocket")
        ws_ok, ws_detail = check_websocket(api_port)
        check("live frames received over /ws", ws_ok, ws_detail)

        # --- summary ------------------------------------------------------
        print()
        if _failures:
            print(f"\033[31m{len(_failures)} check(s) failed:\033[0m")
            for name in _failures:
                print(f"  - {name}")
            return 1

        print("\033[32mAll checks passed — the stack works end to end.\033[0m")
        if args.keep_running:
            print("\n  Stack is up. Ctrl-C to stop.\n")
            print(f"    API docs      {base}/docs")
            print(f"    Health        {base}/api/health")
            print(f"    Live readings {base}/api/readings?limit=5")
            print(f"    Alerts        {base}/api/alerts")
            if api_port == 8000:
                print(
                    "\n    Dashboard: in a SECOND terminal run"
                    "\n        cd frontend && npm install && npm run dev"
                    "\n    then open http://localhost:5173"
                )
            else:
                print(
                    f"\n    NOTE: the dashboard's dev proxy targets :8000, not "
                    f":{api_port}."
                    "\n    Restart with --api-port 8000 to run the UI against this."
                )
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        return 0

    finally:
        for proc in reversed(procs):
            proc.terminate()
        shutil.rmtree(workdir, ignore_errors=True)
        for proc in reversed(procs):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def check_websocket(port: int) -> tuple[bool, str]:
    """Open a raw WebSocket and read a few frames.

    Implemented against the socket directly so the smoke test needs no extra
    dependency beyond what the backend already requires.
    """
    import base64
    import struct

    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(handshake.encode())
        response = sock.recv(4096)
        if b"101" not in response.split(b"\r\n")[0]:
            return False, "handshake rejected"

        types: list[str] = []
        sock.settimeout(12)
        buffer = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and len(types) < 2:
            if len(buffer) < 2:
                buffer += sock.recv(8192)
                continue
            length = buffer[1] & 0x7F
            offset = 2
            if length == 126:
                length = struct.unpack(">H", buffer[2:4])[0]
                offset = 4
            elif length == 127:
                length = struct.unpack(">Q", buffer[2:10])[0]
                offset = 10
            if len(buffer) < offset + length:
                buffer += sock.recv(8192)
                continue
            payload = buffer[offset : offset + length]
            buffer = buffer[offset + length :]
            # A frame we cannot parse is not a failure of the transport,
            # which is what this check is about.
            with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError, KeyError):
                types.append(json.loads(payload.decode())["type"])
        sock.close()
        return bool(types), f"frames: {', '.join(types) or 'none'}"
    except OSError as exc:
        return False, str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
