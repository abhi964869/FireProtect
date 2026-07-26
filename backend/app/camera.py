"""Fire detection from a browser camera node.

Why this exists
---------------
A phone or laptop has no MQ-2, no MQ-135 and no DHT22. There is no web API that
can read smoke, combustible gas, room temperature or humidity — those numbers
only exist if somebody wired up the hardware. What a browser *can* deliver is
video, and video carries real evidence of fire.

So a camera node is a genuinely different sensor, not a stand-in for the ESP32,
and it gets its own classifier rather than being pushed through the random
forest. The forest was trained on nine gas/thermal features; feeding it
camera-derived numbers dressed up as ppm would produce a confident-looking
probability that means nothing. Everything here is instead an explicit,
inspectable rule over quantities the camera actually measures.

What the browser sends (all dimensionless, all in [0, 1])
---------------------------------------------------------
``flame_ratio``
    Fraction of frame pixels passing a flame-colour test — bright, red-dominant
    and warm-hued. The test itself lives in ``frontend/src/lib/cameraSensor.ts``.
``luminance``
    Mean perceptual brightness of the frame. Used for context, not for scoring:
    a dark room and a bright room can both be on fire.
``haze_index``
    Loss of high-frequency detail, i.e. how blurred and washed out the scene is.
    Smoke scatters light and flattens local contrast, so haze rises as a room
    fills. It is also high for an out-of-focus or fogged lens, which is why it
    is scored *relative to a per-device baseline* rather than absolutely.

What the server derives
-----------------------
``flicker``
    Fire is not a steady light. A flame's apparent area oscillates at roughly
    5-15 Hz, so the *variability* of ``flame_ratio`` over a short window is the
    single best discriminator between a real fire and a red jumper, a sunset,
    or a lit "EXIT" sign. Computed here, from the server's own history, so a
    malicious or buggy client cannot fabricate it.
``haze baseline``
    A slow-moving floor for ``haze_index``, so a permanently soft-focus webcam
    settles at "no smoke" instead of alarming forever.

Both need history the client does not have to be trusted with, which is why
they are derived server-side — the same reasoning that puts rate-of-change
features in ``ingest.py`` rather than in the firmware.

Honest by construction
----------------------
This classifier never reports a gas concentration and never invents a
temperature. It answers one question: does the camera see fire? Readings it
produces are tagged ``sensor_kind="camera"`` end to end, and the dashboard
renders them with their own channels so no one can mistake a flicker score for
a smoke-sensor reading.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field

#: Fraction of the frame that must be flame-coloured for full flame evidence.
#: A candle at arm's length covers well under 1% of a frame; a burning
#: wastebasket across a room covers several percent. 5% is "unmistakable".
FLAME_RATIO_FULL = 0.05

#: Below this, a flame-coloured region is treated as noise — JPEG artefacts and
#: a single warm pixel should not move the needle at all.
FLAME_RATIO_FLOOR = 0.0015

#: Coefficient of variation of flame_ratio that counts as full flicker.
#: Real flames routinely exceed 0.4; static objects sit near 0.02.
FLICKER_FULL = 0.30

#: How much flame evidence survives with *zero* flicker. A perfectly steady
#: orange glow is suspicious but is far more likely to be a lamp, a screen or
#: clothing than a fire, so it is capped well below alarm level on its own.
STATIC_FLAME_FLOOR = 0.22

#: Rise in haze_index above the device's own baseline that counts as full
#: smoke evidence.
HAZE_RISE_FULL = 0.22

#: Samples of flame_ratio kept for the flicker estimate. At the default 2 Hz
#: publish rate this is a 6-second window: long enough to average out a single
#: bad frame, short enough to react within one alert debounce cycle.
FLICKER_WINDOW = 12

#: Flicker needs a few samples before its standard deviation means anything.
FLICKER_MIN_SAMPLES = 4

#: EMA weight for the haze baseline. Deliberately slow (~2 minutes to converge
#: at 2 Hz) so that smoke filling a room raises haze *above* the baseline
#: instead of quietly dragging the baseline up with it.
HAZE_BASELINE_ALPHA = 0.01

#: Smoke cannot pull the baseline up at all once it is clearly rising; only
#: readings at or below the current baseline are allowed to lower it quickly.
HAZE_BASELINE_FALL_ALPHA = 0.05

#: Decision thresholds on the derived probabilities.
FIRE_THRESHOLD = 0.55
WARNING_THRESHOLD = 0.32


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


@dataclass
class CameraEvidence:
    """The full, inspectable breakdown behind one camera classification.

    Returned alongside the verdict so the dashboard can show *why* it alarmed
    rather than only a probability. An unexplainable fire alarm is one nobody
    trusts, and one nobody can debug.
    """

    flame_ratio: float
    luminance: float
    haze_index: float
    haze_baseline: float
    flicker: float
    flame_confidence: float
    smoke_confidence: float

    def as_dict(self) -> dict[str, float]:
        return {
            "flame_ratio": round(self.flame_ratio, 6),
            "luminance": round(self.luminance, 6),
            "haze_index": round(self.haze_index, 6),
            "haze_baseline": round(self.haze_baseline, 6),
            "flicker": round(self.flicker, 6),
            "flame_confidence": round(self.flame_confidence, 6),
            "smoke_confidence": round(self.smoke_confidence, 6),
        }


@dataclass
class _CameraState:
    """Per-device rolling history. One instance per camera node."""

    flame_history: deque[float] = field(
        default_factory=lambda: deque(maxlen=FLICKER_WINDOW)
    )
    haze_baseline: float | None = None


def compute_flicker(samples: list[float]) -> float:
    """Coefficient of variation of ``flame_ratio`` over the recent window.

    Normalised by the mean rather than reported as a raw standard deviation:
    a flame occupying 1% of the frame and one occupying 10% flicker by very
    different absolute amounts but are equally obviously fire. Dividing by the
    mean makes the measure scale-free, which is what lets one threshold work at
    any distance from the flame.

    Returns 0.0 when there is not enough history, or when nothing flame-like is
    present — an all-zero window has no meaningful variability.
    """
    if len(samples) < FLICKER_MIN_SAMPLES:
        return 0.0
    mean = sum(samples) / len(samples)
    if mean <= FLAME_RATIO_FLOOR:
        return 0.0
    variance = sum((value - mean) ** 2 for value in samples) / len(samples)
    return math.sqrt(variance) / mean


def _update_haze_baseline(previous: float | None, haze_index: float) -> float:
    """Track the scene's own resting haze level.

    Asymmetric on purpose. Falling haze is adopted quickly (the lens got wiped,
    the room cleared), rising haze is adopted very slowly. If the baseline
    chased smoke upward at the same rate, a room filling gradually would never
    register a rise and the detector would sleep through it — the classic
    failure mode of naive adaptive-background methods.
    """
    if previous is None:
        return haze_index
    alpha = HAZE_BASELINE_ALPHA if haze_index > previous else HAZE_BASELINE_FALL_ALPHA
    return previous + alpha * (haze_index - previous)


class CameraClassifier:
    """Stateful detector: one rolling history per camera device.

    Thread-safe. HTTP handlers for different devices land on different threads
    under uvicorn, and a single device can have two requests in flight if a
    client retries.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _CameraState] = {}

    def reset(self, device_id: str | None = None) -> None:
        with self._lock:
            if device_id is None:
                self._states.clear()
            else:
                self._states.pop(device_id, None)

    def classify(
        self,
        device_id: str,
        flame_ratio: float,
        luminance: float,
        haze_index: float,
    ) -> tuple[str, dict[str, float], CameraEvidence]:
        """Score one frame's metrics.

        Returns ``(status, probabilities, evidence)`` with the same shape as
        ``FireClassifier.predict`` so ``ingest`` can treat both uniformly.
        """
        flame_ratio = _clamp01(flame_ratio)
        luminance = _clamp01(luminance)
        haze_index = _clamp01(haze_index)

        with self._lock:
            state = self._states.setdefault(device_id, _CameraState())
            state.flame_history.append(flame_ratio)
            flicker = compute_flicker(list(state.flame_history))
            state.haze_baseline = _update_haze_baseline(state.haze_baseline, haze_index)
            haze_baseline = state.haze_baseline

        status, probabilities, evidence = score(
            flame_ratio=flame_ratio,
            luminance=luminance,
            haze_index=haze_index,
            haze_baseline=haze_baseline,
            flicker=flicker,
        )
        return status, probabilities, evidence


def score(
    *,
    flame_ratio: float,
    luminance: float,
    haze_index: float,
    haze_baseline: float,
    flicker: float,
) -> tuple[str, dict[str, float], CameraEvidence]:
    """Pure scoring function — no state, fully unit-testable.

    Separated from ``CameraClassifier`` so the decision logic can be tested
    against fixed inputs without constructing rolling history, and so the
    thresholds above can be tuned against a table of cases rather than a live
    camera.
    """
    # --- flame ------------------------------------------------------------
    # Subtract the noise floor before normalising, so a frame with a handful of
    # warm-ish pixels scores a true zero rather than a small positive.
    raw_flame = max(0.0, flame_ratio - FLAME_RATIO_FLOOR)
    flame_area = _clamp01(raw_flame / (FLAME_RATIO_FULL - FLAME_RATIO_FLOOR))
    flicker_gain = _clamp01(flicker / FLICKER_FULL)

    # Area says "something orange is there"; flicker says "and it is alive".
    # Requiring both is what keeps a sunset out of the fire alarm.
    flame_confidence = flame_area * (
        STATIC_FLAME_FLOOR + (1.0 - STATIC_FLAME_FLOOR) * flicker_gain
    )

    # --- smoke ------------------------------------------------------------
    haze_rise = max(0.0, haze_index - haze_baseline)
    smoke_confidence = _clamp01(haze_rise / HAZE_RISE_FULL)

    # --- combine ----------------------------------------------------------
    # FIRE requires visible flame. Haze can only amplify an existing flame
    # signal, never create one: a steamy bathroom is not a fire, and treating
    # haze alone as FIRE would make the feature unusable in a kitchen.
    p_fire = _clamp01(flame_confidence * (1.0 + 0.35 * smoke_confidence))

    # WARNING is the catch-all for "something is off": haze rising on its own,
    # or a flame-coloured region that is not flickering convincingly. This is
    # where the project's bias lives — false positives here are cheap, and the
    # alternative is silence during a smouldering fire that has not flamed yet.
    p_warning = _clamp01(max(smoke_confidence, 0.6 * flame_area, p_fire))

    if p_fire >= FIRE_THRESHOLD:
        status = "FIRE"
    elif p_warning >= WARNING_THRESHOLD:
        status = "WARNING"
    else:
        status = "SAFE"

    # Express as a distribution so it plugs into compute_risk_score and the
    # existing gauge unchanged. WARNING holds whatever concern is not already
    # attributed to FIRE.
    fire_mass = p_fire
    warning_mass = max(0.0, p_warning - p_fire)
    safe_mass = max(0.0, 1.0 - fire_mass - warning_mass)
    total = fire_mass + warning_mass + safe_mass
    probabilities = {
        "SAFE": safe_mass / total,
        "WARNING": warning_mass / total,
        "FIRE": fire_mass / total,
    }

    evidence = CameraEvidence(
        flame_ratio=flame_ratio,
        luminance=luminance,
        haze_index=haze_index,
        haze_baseline=haze_baseline,
        flicker=flicker,
        flame_confidence=flame_confidence,
        smoke_confidence=smoke_confidence,
    )
    return status, probabilities, evidence


def describe(status: str, evidence: CameraEvidence) -> str:
    """Human-readable reason for an alert, for the alert feed.

    Written to be read by someone who has just been alarmed at and wants to
    know whether to run or to go and look.
    """
    if status == "FIRE":
        return (
            f"Camera sees flame: {evidence.flame_ratio * 100:.1f}% of frame "
            f"flame-coloured, flicker {evidence.flicker:.2f}"
        )
    reasons: list[str] = []
    if evidence.smoke_confidence > 0.25:
        rise = evidence.haze_index - evidence.haze_baseline
        reasons.append(f"haze rising {rise:+.2f} above baseline")
    if evidence.flame_ratio > FLAME_RATIO_FLOOR * 4:
        reasons.append(
            f"{evidence.flame_ratio * 100:.1f}% of frame flame-coloured "
            f"(flicker {evidence.flicker:.2f})"
        )
    detail = ", ".join(reasons) if reasons else "camera scene changed"
    return f"Camera warning: {detail}"


_classifier: CameraClassifier | None = None


def get_camera_classifier() -> CameraClassifier:
    global _classifier
    if _classifier is None:
        _classifier = CameraClassifier()
    return _classifier


def reset_camera_classifier() -> None:
    global _classifier
    _classifier = None
