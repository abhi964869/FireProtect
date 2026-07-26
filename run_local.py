#!/usr/bin/env python3
"""Run the whole FireProtect system on localhost. One command, one port.

    python run_local.py

Starts an MQTT broker, the backend (which also serves the dashboard), and a
simulated ESP32 — then opens http://localhost:8000 in your browser.

No Docker. No Node. No second terminal. Nothing to configure.

    python run_local.py --scenario flashover   # watch a fire develop
    python run_local.py --no-simulator         # real hardware only
    python run_local.py --port 9000            # different port
    python run_local.py --no-browser           # do not auto-open

Press Ctrl-C to stop everything.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

GREEN, RED, YELLOW, CYAN, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    # Older Windows consoles render raw escape codes as garbage. Enable ANSI if
    # we can; otherwise drop colour entirely rather than print mojibake.
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7):
            raise OSError
    except Exception:
        GREEN = RED = YELLOW = CYAN = DIM = RESET = ""

OK = f"{GREEN}OK{RESET}"
BAD = f"{RED}!!{RESET}"

_procs: list[subprocess.Popen] = []
_stopping = threading.Event()


def info(message: str) -> None:
    print(f"  {message}", flush=True)


def die(message: str, hint: str = "") -> None:
    print(f"\n{RED}Cannot start:{RESET} {message}", file=sys.stderr)
    if hint:
        print(f"\n{hint}\n", file=sys.stderr)
    _shutdown()
    raise SystemExit(1)


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http(url: str, timeout: float, proc: subprocess.Popen | None = None) -> bool:
    """Poll a URL until it answers. Fails fast if the process has already died."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(0.3)
    return False


def wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


BROKER_SCRIPT = """
import asyncio, sys
from amqtt.broker import Broker

async def main(port):
    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"0.0.0.0:{port}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
        },
    })
    await broker.start()
    await asyncio.Event().wait()

asyncio.run(main(int(sys.argv[1])))
"""


def _shutdown(*_args: object) -> None:
    if _stopping.is_set():
        return
    _stopping.set()
    for proc in reversed(_procs):
        if proc.poll() is None:
            proc.terminate()
    for proc in reversed(_procs):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def preflight(args: argparse.Namespace) -> Path:
    """Check every prerequisite up front, with an actionable message for each."""
    print(f"\n{CYAN}FireProtect{RESET} — starting locally\n")

    # Guarded at runtime, not with a version block: this script is the first
    # thing a user runs, and it must give a clear message on an old interpreter
    # rather than a SyntaxError from a downstream module.
    required = (3, 10)
    if tuple(sys.version_info[:2]) < required:
        die(
            f"Python {required[0]}.{required[1]}+ required, "
            f"found {sys.version.split()[0]}",
            "Install a newer Python from https://python.org and re-run.",
        )

    missing = []
    for module, package in (
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("sqlalchemy", "SQLAlchemy"),
        ("paho.mqtt.client", "paho-mqtt"),
        ("pydantic_settings", "pydantic-settings"),
        ("sklearn", "scikit-learn"),
        ("numpy", "numpy"),
        ("pandas", "pandas"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        die(
            f"missing Python packages: {', '.join(missing)}",
            "Install them with:\n\n"
            "    pip install -r backend/requirements-dev.txt",
        )
    info(f"[{OK}] Python {sys.version.split()[0]} and dependencies")

    try:
        __import__("amqtt.broker")
        broker_available = True
    except ImportError:
        broker_available = False
    if not broker_available and not args.no_broker:
        die(
            "the bundled MQTT broker (amqtt) is not installed",
            "Install it with:\n\n"
            "    pip install amqtt\n\n"
            "Or run without MQTT (the simulator will post over HTTP instead):\n\n"
            "    python run_local.py --no-broker",
        )

    model = REPO_ROOT / "ml" / "random_forest.joblib"
    if model.is_file():
        info(f"[{OK}] trained model found")
    else:
        info(
            f"[{YELLOW}--{RESET}] no trained model — the backend will fall back to "
            "threshold rules"
        )
        info(f"     {DIM}build it with: python ml/train.py{RESET}")

    dist = REPO_ROOT / "frontend" / "dist"
    if (dist / "index.html").is_file() and not dashboard_is_stale(dist):
        info(f"[{OK}] dashboard build found")
    elif (dist / "index.html").is_file():
        # Committed build output is convenient but can drift behind the source.
        # Rebuild if we can; otherwise say so plainly rather than serving a UI
        # that silently does not match the code.
        if try_build_frontend():
            info(f"[{OK}] dashboard rebuilt (sources were newer)")
        else:
            info(
                f"[{YELLOW}--{RESET}] dashboard build is older than the sources and "
                "Node is unavailable"
            )
            info(f"     {DIM}serving the committed build; it may not match src/{RESET}")
    else:
        built = try_build_frontend()
        if not built:
            die(
                "the dashboard has not been built and Node is unavailable",
                "Either install Node 18+ and run:\n\n"
                "    cd frontend && npm install && npm run build\n\n"
                "…or use the API only (Swagger UI at /docs):\n\n"
                "    python run_local.py --api-only",
            )
        info(f"[{OK}] dashboard built")

    if not port_is_free(args.port):
        die(
            f"port {args.port} is already in use",
            f"Something else is listening on {args.port}. Either stop it, or run:\n\n"
            f"    python run_local.py --port {free_port()}",
        )
    info(f"[{OK}] port {args.port} is free")
    return dist


def sqlite_can_write(path: Path) -> str | None:
    """Return None if SQLite can create and write a database at ``path``.

    SQLite needs real POSIX/Windows file locking. Network shares (SMB/NFS),
    cloud-synced folders (OneDrive, Dropbox, Google Drive) and some
    virtualised mounts do not provide it, and fail with "disk I/O error" or
    "database is locked" — or worse, hang. Probing up front turns a confusing
    60-second startup stall into an immediate, explainable fallback.
    """
    probe = path.parent / f".fireprotect-probe-{os.getpid()}.db"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(probe), timeout=3)
        try:
            connection.execute("CREATE TABLE probe (x INTEGER)")
            connection.execute("INSERT INTO probe VALUES (1)")
            connection.commit()
        finally:
            connection.close()
        return None
    except Exception as exc:
        return str(exc)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(OSError):
                Path(str(probe) + suffix).unlink()


def choose_database_path() -> Path:
    """Prefer the repo, fall back to temp if the filesystem cannot host SQLite."""
    preferred = REPO_ROOT / "backend" / "fireprotect.db"
    problem = sqlite_can_write(preferred)
    if problem is None:
        return preferred

    fallback = Path(tempfile.gettempdir()) / "fireprotect" / "fireprotect.db"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    info(
        f"[{YELLOW}--{RESET}] this folder cannot host a SQLite database "
        f"({problem})"
    )
    info(f"     {DIM}using {fallback} instead — data will not persist "
         f"in the project folder{RESET}")
    info(f"     {DIM}(common on network drives and cloud-synced folders){RESET}")
    return fallback


def dashboard_is_stale(dist: Path) -> bool:
    """True if any frontend source is newer than the built index.html."""
    index = dist / "index.html"
    if not index.is_file():
        return True
    built_at = index.stat().st_mtime
    roots = [
        REPO_ROOT / "frontend" / "src",
        REPO_ROOT / "frontend" / "public",
        REPO_ROOT / "frontend" / "index.html",
        REPO_ROOT / "frontend" / "tailwind.config.js",
        REPO_ROOT / "frontend" / "vite.config.ts",
    ]
    for root in roots:
        if root.is_file():
            if root.stat().st_mtime > built_at:
                return True
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.stat().st_mtime > built_at:
                    return True
    return False


def try_build_frontend() -> bool:
    """Build the dashboard if npm is available. Returns True on success."""
    npm = shutil.which("npm")
    if npm is None:
        return False
    frontend = REPO_ROOT / "frontend"
    info(f"[{YELLOW}--{RESET}] dashboard not built; building it now (one-off, ~1 min)")
    try:
        if not (frontend / "node_modules").is_dir():
            info(f"     {DIM}npm install…{RESET}")
            subprocess.run(
                [npm, "install", "--no-audit", "--no-fund"],
                cwd=frontend, check=True, capture_output=True, timeout=900,
            )
        info(f"     {DIM}npm run build…{RESET}")
        subprocess.run(
            [npm, "run", "build"],
            cwd=frontend, check=True, capture_output=True, timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        output = getattr(exc, "stderr", b"") or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        print(f"\n{RED}Frontend build failed:{RESET}\n{output[-2000:]}", file=sys.stderr)
        return False
    return (frontend / "dist" / "index.html").is_file()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenario",
        choices=("normal", "smoldering", "flashover"),
        default="normal",
        help="what the simulated device does (default: normal)",
    )
    parser.add_argument("--port", type=int, default=8000, help="dashboard + API port")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--no-simulator", action="store_true")
    parser.add_argument(
        "--no-broker",
        action="store_true",
        help="skip MQTT; the simulator posts to the REST endpoint instead",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--api-only", action="store_true", help="do not require a dashboard build"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=8.0,
        help="how many times faster than real time the scenario runs (default: 8)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.api_only:
        print(f"\n{CYAN}FireProtect{RESET} — starting locally (API only)\n")
    else:
        preflight(args)

    mqtt_port = args.mqtt_port
    use_broker = not args.no_broker
    if use_broker and not port_is_free(mqtt_port):
        info(
            f"[{YELLOW}--{RESET}] port {mqtt_port} busy — assuming a broker is already "
            "running there"
        )

    db_path = choose_database_path()

    env = os.environ.copy()
    env.update(
        {
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": str(mqtt_port),
            "MODEL_DIR": str(REPO_ROOT / "ml"),
            "FRONTEND_DIST": str(REPO_ROOT / "frontend" / "dist"),
            "DATABASE_URL": f"sqlite:///{db_path}",
            "THINGSPEAK_BUFFER_PATH": str(db_path.parent / "thingspeak_buffer.jsonl"),
            "PYTHONPATH": str(REPO_ROOT / "backend"),
            "PYTHONUNBUFFERED": "1",
        }
    )

    print()

    # --- broker -----------------------------------------------------------
    if use_broker and port_is_free(mqtt_port):
        _procs.append(
            subprocess.Popen(
                [sys.executable, "-c", BROKER_SCRIPT, str(mqtt_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=REPO_ROOT,
            )
        )
        if wait_for_port(mqtt_port, 20):
            info(f"[{OK}] MQTT broker      127.0.0.1:{mqtt_port}")
        else:
            info(f"[{BAD}] MQTT broker failed to start — continuing without it")
            use_broker = False

    # --- backend ----------------------------------------------------------
    # Backend output goes to a file, not DEVNULL: if startup fails, the reason
    # is the single most useful thing we can show, and "it did not start" with
    # no explanation is a dead end for whoever is running this.
    log_path = Path(tempfile.gettempdir()) / "fireprotect-backend.log"
    log_handle = log_path.open("w", encoding="utf-8")
    backend = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "info",
        ],
        cwd=REPO_ROOT / "backend", env=env,
        stdout=log_handle, stderr=subprocess.STDOUT,
    )
    _procs.append(backend)

    base = f"http://127.0.0.1:{args.port}"
    if not wait_for_http(f"{base}/api/health", 60, backend):
        log_handle.flush()
        tail = ""
        with contextlib.suppress(OSError):
            tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-25:])
        die(
            "the backend did not start",
            (f"Last output from the backend:\n\n{tail}\n" if tail.strip() else "")
            + f"\nFull log: {log_path}\n\n"
            "Run it directly to reproduce:\n\n"
            "    cd backend\n"
            f"    uvicorn app.main:app --port {args.port}",
        )
    info(f"[{OK}] backend + dashboard {base}")

    # --- simulator --------------------------------------------------------
    if not args.no_simulator:
        interval = max(0.1, 2.0 / max(args.speed, 0.1))
        cmd = [
            sys.executable, "simulator/virtual_device.py",
            "--scenario", args.scenario,
            "--device-id", f"esp32-sim-{args.scenario}",
            "--location", "Living Room",
            "--interval", f"{interval:.3f}", "--sim-step", "2.0", "--quiet",
        ]
        if use_broker:
            cmd += ["--host", "127.0.0.1", "--port", str(mqtt_port)]
        else:
            cmd += ["--transport", "http", "--api-url", base]
        _procs.append(
            subprocess.Popen(
                cmd, cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        )
        transport = "MQTT" if use_broker else "HTTP"
        info(
            f"[{OK}] simulator        {args.scenario} over {transport} "
            f"({args.speed:.0f}x speed)"
        )

    # --- ready ------------------------------------------------------------
    print(f"\n{GREEN}Running.{RESET}  Open {CYAN}{base}{RESET}\n")
    print(f"    Dashboard   {base}")
    print(f"    API docs    {base}/docs")
    print(f"    Health      {base}/api/health")
    if args.scenario == "flashover":
        print(f"\n    {DIM}The fire takes ~15s to develop. Watch the hero go "
              f"SAFE → WARNING → FIRE.{RESET}")
    print(f"\n{DIM}Ctrl-C to stop.{RESET}\n")

    if not args.no_browser:
        # Give uvicorn a beat to be fully ready before the browser hits it.
        threading.Timer(1.0, lambda: webbrowser.open(base)).start()

    try:
        while not _stopping.is_set():
            if backend.poll() is not None:
                print(f"\n{RED}Backend exited unexpectedly.{RESET}", file=sys.stderr)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping…")
        _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
