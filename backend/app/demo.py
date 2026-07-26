"""Self-driving demo telemetry.

A deployed instance has no ESP32 and nobody running the simulator, so without
this the dashboard is permanently empty for anyone who opens the link. When
``DEMO_MODE`` is on, the backend generates its own telemetry in-process and
feeds it through the *real* ingest path — same validation, same random forest,
same alerting, same WebSocket broadcast. Nothing is faked downstream of the
sensor model.

The physics is the same code the standalone simulator uses
(``simulator/virtual_device.py``), imported rather than duplicated so the two
can never drift apart.

Cycle: the device runs normally for a while, then a fire develops, then it
resets. That way a visitor arriving at any moment sees either a healthy system
or one in alarm — both worth looking at — rather than a flat line forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.ingest import get_ingest_service
from app.schemas import TelemetryIn

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_virtual_device() -> Any:
    """Import the simulator's VirtualDevice, or return None if unavailable.

    The simulator lives outside the backend package and is not shipped in every
    deployment image. Demo mode is a nice-to-have, so a missing simulator must
    degrade to "no demo data", never to a crashed application.
    """
    for candidate in (REPO_ROOT / "simulator", REPO_ROOT.parent / "simulator"):
        if (candidate / "virtual_device.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            break
    try:
        from virtual_device import VirtualDevice  # type: ignore[import-not-found]
    except Exception:
        logger.warning(
            "DEMO_MODE is on but simulator/virtual_device.py could not be "
            "imported; the dashboard will have no demo data"
        )
        return None
    return VirtualDevice


class DemoDriver:
    """Drives one simulated device through a repeating narrative."""

    #: Scenario order. `normal` first so a fresh instance opens on a calm
    #: dashboard, then an escalating event, then calm again.
    CYCLE: tuple[tuple[str, int], ...] = (
        ("normal", 90),
        ("flashover", 70),
        ("normal", 60),
        ("smoldering", 110),
    )

    def __init__(self) -> None:
        self._settings = get_settings()
        self._device_factory = _load_virtual_device()
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        return self._device_factory is not None

    async def _run(self) -> None:
        service = get_ingest_service()
        interval = self._settings.demo_interval_s
        sim_step = self._settings.demo_sim_step_s
        device_id = self._settings.demo_device_id
        rng = random.Random()

        logger.info(
            "demo mode: publishing as %s every %.1fs (%.0fx simulated speed)",
            device_id,
            interval,
            sim_step / interval if interval else 1.0,
        )

        while True:
            for scenario, ticks in self.CYCLE:
                device = self._device_factory(  # type: ignore[misc]
                    device_id,
                    scenario,
                    seed=rng.randrange(1_000_000),
                    location=self._settings.demo_location,
                )
                for _ in range(ticks):
                    try:
                        payload = device.tick(sim_step)
                        telemetry = TelemetryIn.model_validate(payload)
                        # to_thread: ingest does blocking SQLAlchemy work and
                        # sklearn inference; running it inline would stall the
                        # event loop and stutter every other request.
                        await asyncio.to_thread(service.process_telemetry, telemetry)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("demo tick failed; continuing")
                    await asyncio.sleep(interval)

    async def start(self) -> None:
        if not self.available:
            return
        self._task = asyncio.create_task(self._run(), name="fireprotect-demo")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        # Shutdown must not be blocked by however the task chose to die.
        with contextlib.suppress(BaseException):
            await self._task
        self._task = None
