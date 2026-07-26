"""Virtual ESP32 device - publishes realistic telemetry over MQTT.

The project has no physical hardware, so this stands in for it. It reuses the
*same* physics model as the training-data generator, so what the backend sees
is drawn from the same distribution the models were fitted on, and it drives the
same MQTT topics real firmware would.

Three scenarios:

* ``normal``     - quiescent baseline with drift, occasional cooking / steam
                   nuisance events. Expected outcome: SAFE (occasional WARNING).
* ``smoldering`` - slow pyrolysis ramp, heavy particulates, no flame.
                   Expected outcome: escalates to WARNING, then FIRE.
* ``flashover``  - fast flaming fire with strong IR signature.
                   Expected outcome: escalates to FIRE quickly.

Usage::

    python simulator/virtual_device.py --scenario flashover --device-id esp32-sim-01
    python simulator/virtual_device.py --scenario normal --duration 120
    python simulator/virtual_device.py --scenario flashover --transport http
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "ml"))

import numpy as np  # noqa: E402
from generate_dataset import (  # noqa: E402
    MQ2_A,
    MQ2_B,
    MQ2_MAX_PPM,
    MQ135_A,
    MQ135_B,
    MQ135_MAX_PPM,
    adc_quantise,
    compute_heat_index,
    mq_ratio_to_ppm,
)

logger = logging.getLogger("simulator")

SCENARIOS = ("normal", "smoldering", "flashover")

#: On-device decision-tree stand-in thresholds. The real firmware runs the
#: exported C tree; the simulator approximates it so the ``device_status`` field
#: is populated with something plausible rather than a constant.
DEVICE_FIRE_TEMP_C = 58.0
DEVICE_FIRE_SMOKE_PPM = 2200.0
DEVICE_WARN_TEMP_C = 45.0
DEVICE_WARN_SMOKE_PPM = 900.0
DEVICE_FLAME_VOLTS = 1.15


@dataclass
class SensorState:
    """Mutable physical state of the simulated room.

    Baseline and event contributions are tracked *separately* on purpose. An
    earlier version applied ambient mean-reversion to the total temperature,
    which meant the reversion term fought the fire's own heat release and the
    flashover scenario stalled around 45 C instead of climbing. Physically, a
    burning room does not revert to ambient; only the baseline does.
    """

    #: Ambient conditions - drift and revert toward the diurnal set point.
    baseline_temp_c: float = 22.0
    baseline_humidity_pct: float = 45.0
    #: Event contributions - added on top, decaying only when not driven.
    excess_temp_c: float = 0.0
    excess_humidity_pct: float = 0.0
    #: MQ baselines drift around 1.0; depression is driven by the event.
    mq2_baseline: float = 1.0
    mq135_baseline: float = 1.0
    mq2_depression: float = 0.0
    mq135_depression: float = 0.0
    flame_irradiance: float = 0.0
    elapsed_s: float = 0.0

    @property
    def temperature_c(self) -> float:
        return self.baseline_temp_c + self.excess_temp_c

    @property
    def humidity_pct(self) -> float:
        return min(
            100.0, max(1.0, self.baseline_humidity_pct + self.excess_humidity_pct)
        )

    @property
    def mq2_ratio(self) -> float:
        """Effective Rs/R0. Depression is multiplicative, so it cannot go < 0."""
        return min(3.0, max(0.05, self.mq2_baseline * (1.0 - self.mq2_depression)))

    @property
    def mq135_ratio(self) -> float:
        return min(3.0, max(0.05, self.mq135_baseline * (1.0 - self.mq135_depression)))


class VirtualDevice:
    """Generates one telemetry sample per tick for a given scenario."""

    def __init__(
        self,
        device_id: str,
        scenario: str,
        seed: int | None = None,
        location: str = "Simulated Room",
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {SCENARIOS}, got {scenario!r}")
        self.device_id = device_id
        self.scenario = scenario
        self.location = location
        self.state = SensorState()
        self.rng = random.Random(seed)
        self._np_rng = np.random.default_rng(seed)
        #: Set when a nuisance event is active in the ``normal`` scenario.
        self._event: str | None = None
        self._event_remaining_s = 0.0

    # -- physics -----------------------------------------------------------

    def _drift(self) -> None:
        """Advance ambient baselines and decay event contributions.

        Only the *baseline* reverts to ambient. Event excess decays on its own
        much more slowly, so an active fire keeps building instead of being
        pulled back to room temperature.
        """
        state = self.state

        # Ornstein-Uhlenbeck baseline drift on the chemiresistive sensors.
        state.mq2_baseline += 0.02 * (1.0 - state.mq2_baseline)
        state.mq2_baseline += self.rng.gauss(0.0, 0.010)
        state.mq135_baseline += 0.02 * (1.0 - state.mq135_baseline)
        state.mq135_baseline += self.rng.gauss(0.0, 0.008)

        # Diurnal ambient swing plus reversion to the ambient set point.
        diurnal = 3.0 * math.sin(2 * math.pi * state.elapsed_s / 86400.0)
        state.baseline_temp_c += 0.05 * (22.0 + diurnal - state.baseline_temp_c)
        state.baseline_temp_c += self.rng.gauss(0.0, 0.05)
        state.baseline_humidity_pct += 0.05 * (45.0 - state.baseline_humidity_pct)
        state.baseline_humidity_pct += self.rng.gauss(0.0, 0.15)

        # Event contributions cool / clear slowly once no longer driven.
        state.excess_temp_c *= 0.97
        state.excess_humidity_pct *= 0.95
        state.mq2_depression *= 0.985
        state.mq135_depression *= 0.985
        state.flame_irradiance *= 0.85

    def _apply_normal(self, dt: float) -> None:
        """Quiescent with occasional cooking / steam nuisance events."""
        state = self.state
        if self._event_remaining_s > 0:
            self._event_remaining_s -= dt
            if self._event == "cooking":
                # Bounded severity: frying makes smoke and heat, but must stay
                # clear of the FIRE corroboration rule (no flame IR, capped
                # depression) or the detector is useless in a real kitchen.
                state.mq2_depression = min(0.34, state.mq2_depression + 0.010)
                state.mq135_depression = min(0.26, state.mq135_depression + 0.008)
                state.excess_temp_c = min(7.0, state.excess_temp_c + 0.20)
                state.excess_humidity_pct = min(
                    12.0, state.excess_humidity_pct + 0.35
                )
                state.flame_irradiance = max(
                    state.flame_irradiance, 0.06 + self.rng.uniform(0, 0.04)
                )
            elif self._event == "steam":
                state.excess_humidity_pct = min(
                    45.0, state.excess_humidity_pct + 1.4
                )
                state.excess_temp_c = min(3.5, state.excess_temp_c + 0.09)
                state.mq2_depression = min(0.08, state.mq2_depression + 0.003)
                state.mq135_depression = min(0.13, state.mq135_depression + 0.005)
            if self._event_remaining_s <= 0:
                logger.info("nuisance event %r ended", self._event)
                self._event = None
        elif self.rng.random() < 0.008:
            self._event = self.rng.choice(["cooking", "steam"])
            self._event_remaining_s = self.rng.uniform(40.0, 120.0)
            logger.info(
                "nuisance event %r started for %.0fs (expect SAFE/WARNING, never FIRE)",
                self._event,
                self._event_remaining_s,
            )

    def _apply_smoldering(self, dt: float) -> None:
        """Slow pyrolysis: particulates first, heat later, no flame."""
        # 30 s of normal baseline before the event begins, so the dashboard
        # shows a clean baseline to escalate from.
        if self.state.elapsed_s < 30.0:
            return
        state = self.state
        progress = min(1.0, (state.elapsed_s - 30.0) / 240.0)
        # Particulates lead, heat follows - the signature of pyrolysis.
        state.mq2_depression = min(0.88, state.mq2_depression + 0.010 * (0.3 + progress))
        state.mq135_depression = min(
            0.80, state.mq135_depression + 0.008 * (0.3 + progress)
        )
        state.excess_temp_c += 0.45 * progress
        state.excess_humidity_pct -= 0.06 * progress
        # No flame: smoldering is combustion without an open flame front.
        state.flame_irradiance = max(state.flame_irradiance, 0.05 * progress)

    def _apply_flashover(self, dt: float) -> None:
        """Fast flaming fire: exponential heat release and strong IR."""
        if self.state.elapsed_s < 20.0:
            return
        state = self.state
        progress = min(1.0, (state.elapsed_s - 20.0) / 90.0)
        ramp = math.expm1(3.2 * progress) / math.expm1(3.2)
        state.mq2_depression = min(0.94, state.mq2_depression + 0.030 * (0.2 + ramp))
        state.mq135_depression = min(
            0.90, state.mq135_depression + 0.025 * (0.2 + ramp)
        )
        # Exponential heat release: a real flashover takes a room from ambient
        # to well over 100 C in a couple of minutes.
        state.excess_temp_c += 2.6 * ramp
        state.excess_humidity_pct -= 0.45 * ramp
        state.flame_irradiance = min(0.98, 0.15 + 0.85 * ramp)

    def tick(self, dt: float = 2.0) -> dict[str, Any]:
        """Advance the simulation and return one telemetry payload."""
        self.state.elapsed_s += dt
        self._drift()

        if self.scenario == "normal":
            self._apply_normal(dt)
        elif self.scenario == "smoldering":
            self._apply_smoldering(dt)
        else:
            self._apply_flashover(dt)

        # Clamp the mutable components. The ratio/temperature/humidity
        # properties clamp their own composed output.
        state = self.state
        state.mq2_depression = min(0.95, max(0.0, state.mq2_depression))
        state.mq135_depression = min(0.95, max(0.0, state.mq135_depression))
        state.excess_temp_c = min(260.0, max(-15.0, state.excess_temp_c))
        state.excess_humidity_pct = min(55.0, max(-40.0, state.excess_humidity_pct))
        state.baseline_temp_c = min(60.0, max(-20.0, state.baseline_temp_c))
        state.flame_irradiance = min(1.0, max(0.0, state.flame_irradiance))

        return self._build_payload()

    def _build_payload(self) -> dict[str, Any]:
        state = self.state
        # Saturate at the datasheet ceiling, exactly as the training generator
        # does - a real MQ sensor pins at full scale rather than reading higher.
        smoke_ppm = min(
            MQ2_MAX_PPM,
            float(mq_ratio_to_ppm(np.array([state.mq2_ratio]), MQ2_A, MQ2_B)[0]),
        )
        air_ppm = min(
            MQ135_MAX_PPM,
            float(mq_ratio_to_ppm(np.array([state.mq135_ratio]), MQ135_A, MQ135_B)[0]),
        )
        flame_volts = float(
            adc_quantise(np.array([min(3.3, 3.3 * state.flame_irradiance)]))[0]
        )

        temperature = round(state.temperature_c + self._np_rng.normal(0, 0.17), 1)
        humidity = round(
            min(100.0, max(0.0, state.humidity_pct + self._np_rng.normal(0, 0.67))), 1
        )

        return {
            "device_id": self.device_id,
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "smoke_ppm": round(smoke_ppm, 3),
            "air_quality_ppm": round(air_ppm, 3),
            "flame_analog_volts": round(flame_volts, 4),
            "flame_detected": int(state.flame_irradiance > 0.35),
            "device_status": self._local_classification(
                temperature, smoke_ppm, air_ppm, flame_volts
            ),
            "firmware_version": "1.0.0-sim",
            "location": self.location,
            "device_uptime_s": round(state.elapsed_s, 1),
            "heat_index_c": round(compute_heat_index(
                np.array([temperature]), np.array([humidity])
            )[0], 2),
        }

    def _local_classification(
        self, temperature: float, smoke: float, air: float, flame_volts: float
    ) -> str:
        """Approximates the on-device decision tree's verdict."""
        flame = flame_volts > DEVICE_FLAME_VOLTS
        hot = temperature > DEVICE_WARN_TEMP_C
        very_hot = temperature > DEVICE_FIRE_TEMP_C
        heavy = smoke > DEVICE_WARN_SMOKE_PPM
        severe = smoke > DEVICE_FIRE_SMOKE_PPM

        if (flame and (hot or heavy)) or (very_hot and heavy) or severe:
            return "FIRE"
        if hot or heavy or flame or air > 300.0:
            return "WARNING"
        return "SAFE"


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class MqttTransport:
    """Publishes to an MQTT broker, matching what real firmware would do."""

    def __init__(self, host: str, port: int, prefix: str = "fireprotect") -> None:
        import paho.mqtt.client as mqtt

        self.prefix = prefix
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:  # pragma: no cover - paho < 2.0
            self.client = mqtt.Client()
        self.client.connect(host, port, 60)
        self.client.loop_start()
        logger.info("connected to MQTT broker %s:%s", host, port)

    def publish(self, payload: dict[str, Any]) -> None:
        topic = f"{self.prefix}/{payload['device_id']}/telemetry"
        self.client.publish(topic, json.dumps(payload), qos=1)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class HttpTransport:
    """Posts straight to the backend's REST ingest endpoint.

    Useful for demonstrating the pipeline without running a broker at all.
    """

    def __init__(self, base_url: str) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=10.0)
        logger.info("posting telemetry to %s/api/telemetry", self.base_url)

    def publish(self, payload: dict[str, Any]) -> None:
        response = self.client.post(f"{self.base_url}/api/telemetry", json=payload)
        if response.status_code >= 400:
            logger.error("backend rejected telemetry: %s", response.text[:200])

    def close(self) -> None:
        self.client.close()


class StdoutTransport:
    """Prints payloads. Used for smoke-testing the generator alone."""

    def publish(self, payload: dict[str, Any]) -> None:
        print(json.dumps(payload))

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_stop = False


def _handle_signal(_signum: int, _frame: FrameType | None) -> None:
    global _stop
    _stop = True
    logger.info("stopping...")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--device-id", default="esp32-sim-01")
    parser.add_argument("--location", default="Simulated Room")
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--prefix", default="fireprotect")
    parser.add_argument(
        "--transport", choices=("mqtt", "http", "stdout"), default="mqtt"
    )
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="wall-clock seconds between published messages",
    )
    parser.add_argument(
        "--sim-step",
        type=float,
        default=None,
        help=(
            "simulated seconds advanced per tick (default: same as --interval). "
            "Set this to decouple simulated time from wall-clock time - e.g. "
            "`--interval 0.25 --sim-step 2` replays a fire 8x faster without "
            "changing its shape. Lowering --interval alone also slows the "
            "scenario down, because the ramps are driven by simulated time."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="seconds to run; 0 means run until interrupted",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    device = VirtualDevice(
        args.device_id, args.scenario, seed=args.seed, location=args.location
    )

    transport: Any
    if args.transport == "mqtt":
        try:
            transport = MqttTransport(args.host, args.port, args.prefix)
        except OSError as exc:
            logger.error(
                "cannot reach MQTT broker at %s:%s (%s). Start it with "
                "`docker compose up mosquitto`, or use --transport http.",
                args.host,
                args.port,
                exc,
            )
            return 1
    elif args.transport == "http":
        transport = HttpTransport(args.api_url)
    else:
        transport = StdoutTransport()

    sim_step = args.sim_step if args.sim_step is not None else args.interval
    if sim_step != args.interval:
        logger.info(
            "simulating %r as %s: publishing every %.2fs, %.2fs simulated per tick "
            "(%.1fx speed)",
            args.scenario, args.device_id, args.interval, sim_step,
            sim_step / args.interval,
        )
    else:
        logger.info(
            "simulating %r as %s every %.1fs",
            args.scenario, args.device_id, args.interval,
        )

    published = 0
    started = time.monotonic()
    try:
        while not _stop:
            payload = device.tick(sim_step)
            transport.publish(payload)
            published += 1

            if not args.quiet and published % 5 == 0:
                logger.info(
                    "t=%5.0fs  %.1fC  %.0f ppm  flame %.2fV  -> %s",
                    device.state.elapsed_s,
                    payload["temperature_c"],
                    payload["smoke_ppm"],
                    payload["flame_analog_volts"],
                    payload["device_status"],
                )

            if args.duration and (time.monotonic() - started) >= args.duration:
                break
            time.sleep(args.interval)
    finally:
        transport.close()

    logger.info("published %d messages", published)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
