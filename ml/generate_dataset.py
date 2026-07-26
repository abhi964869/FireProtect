"""Synthetic sensor dataset generator for FireProtect.

Produces a physically plausible multivariate time series from four sensors
(MQ-2 smoke/gas, MQ-135 air quality, DHT22 temperature + humidity, IR flame)
across five regimes: quiescent baseline, cooking/steam nuisance, smoldering
fire, fast-flame fire, and post-fire decay.

Every modelling assumption is documented in ``ml/DATA.md``. The generator is
deterministic given ``--seed`` so that dataset, models and the exported C
decision tree are all reproducible.

Usage::

    python ml/generate_dataset.py --rows 24000 --seed 20260726 --out ml/dataset.csv
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Physical constants and sensor characteristics
# ---------------------------------------------------------------------------

#: Sampling interval in seconds. The ESP32 firmware publishes at this rate.
SAMPLE_PERIOD_S = 2.0

#: Class labels. Ordinal severity ordering matters for the risk gauge.
LABELS = ("SAFE", "WARNING", "FIRE")
LABEL_TO_INT = {name: idx for idx, name in enumerate(LABELS)}

#: MQ-2 clean-air baseline ratio Rs/R0 and the ppm conversion curve constants.
#: Datasheet log-log fit for LPG/smoke: ppm = a * (Rs/R0) ** b
MQ2_A, MQ2_B = 574.25, -2.222
MQ2_R0_NOMINAL = 9.83  # kOhm in clean air

#: MQ-135 CO2-equivalent curve constants (datasheet log-log fit).
MQ135_A, MQ135_B = 116.6020682, -2.769034857
MQ135_R0_NOMINAL = 76.63

#: Datasheet detection ceilings. Real MQ sensors do not report unbounded ppm -
#: they saturate at the top of their measurement range and read no higher. The
#: log-log curve is asymptotic, so without these clamps a strongly depressed
#: Rs/R0 produces physically impossible values (observed: 262,000 ppm), which
#: both misrepresents the sensor and violates the backend's ingest contract.
MQ2_MAX_PPM = 10_000.0  # MQ-2 datasheet range: 300 - 10,000 ppm
MQ135_MAX_PPM = 10_000.0  # MQ-135 CO2-equivalent practical ceiling

#: DHT22 accuracy envelope from the datasheet.
DHT22_TEMP_NOISE_C = 0.5
DHT22_RH_NOISE_PCT = 2.0

#: Ambient conditions for a typical indoor room.
AMBIENT_TEMP_C = 22.0
AMBIENT_RH_PCT = 45.0
DIURNAL_AMPLITUDE_C = 3.0
DIURNAL_PERIOD_S = 24 * 3600.0


@dataclass(frozen=True)
class Regime:
    """A labelled segment of sensor behaviour."""

    name: str
    label: str
    min_duration_s: float
    max_duration_s: float
    weight: float


#: Regime mix. Weights are the relative probability of entering each regime.
#: SAFE dominates because real deployments are overwhelmingly quiescent; the
#: nuisance regimes are over-represented relative to reality so the models
#: learn to reject them rather than treating any smoke as fire.
REGIMES: tuple[Regime, ...] = (
    Regime("quiescent", "SAFE", 120.0, 900.0, 0.34),
    Regime("occupancy", "SAFE", 60.0, 420.0, 0.12),
    Regime("cooking", "WARNING", 90.0, 480.0, 0.14),
    Regime("steam", "SAFE", 60.0, 300.0, 0.10),
    Regime("smoldering", "WARNING", 120.0, 600.0, 0.12),
    Regime("flaming", "FIRE", 60.0, 360.0, 0.12),
    Regime("post_fire", "WARNING", 60.0, 300.0, 0.06),
)


# ---------------------------------------------------------------------------
# Sensor response models
# ---------------------------------------------------------------------------


def mq_ratio_to_ppm(ratio: np.ndarray, a: float, b: float) -> np.ndarray:
    """Convert an Rs/R0 ratio to a ppm reading via the datasheet power law."""
    safe_ratio = np.clip(ratio, 1e-3, None)
    return np.asarray(a * np.power(safe_ratio, b), dtype=np.float64)


def adc_quantise(volts: np.ndarray, bits: int = 12, vref: float = 3.3) -> np.ndarray:
    """Quantise an analogue voltage the way the ESP32 SAR ADC does.

    The ESP32 ADC is 12-bit and noticeably non-linear near the rails; we clip
    to the usable 0.15 V - 3.1 V window before quantising, which is the range
    the firmware actually trusts.
    """
    usable = np.clip(volts, 0.15, 3.1)
    counts = np.round(usable / vref * (2**bits - 1))
    return np.asarray(counts / (2**bits - 1) * vref, dtype=np.float64)


def _ou_process(
    rng: np.random.Generator, n: int, theta: float, sigma: float
) -> np.ndarray:
    """Ornstein-Uhlenbeck noise - models slow correlated sensor drift.

    White noise is unrealistic for chemiresistive sensors; their baseline
    wanders. An OU process gives mean-reverting correlated drift.
    """
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        out[i] = out[i - 1] + theta * (0.0 - out[i - 1]) + sigma * rng.standard_normal()
    return out


# ---------------------------------------------------------------------------
# Regime simulators
# ---------------------------------------------------------------------------


def _simulate_regime(
    rng: np.random.Generator, regime: Regime, n: int, t_abs: np.ndarray
) -> dict[str, np.ndarray]:
    """Return raw physical quantities for one regime segment of ``n`` samples."""
    progress = np.linspace(0.0, 1.0, n) if n > 1 else np.zeros(1)

    # Diurnal ambient temperature swing, shared by every regime.
    diurnal = DIURNAL_AMPLITUDE_C * np.sin(2 * math.pi * t_abs / DIURNAL_PERIOD_S)
    base_temp = AMBIENT_TEMP_C + diurnal
    base_rh = AMBIENT_RH_PCT - 0.6 * diurnal  # RH is anti-correlated with T

    # Correlated baseline drift on each chemiresistive sensor.
    mq2_drift = _ou_process(rng, n, theta=0.02, sigma=0.010)
    mq135_drift = _ou_process(rng, n, theta=0.02, sigma=0.008)

    temp = base_temp.copy()
    rh = base_rh.copy()
    mq2_ratio = np.full(n, 1.0) + mq2_drift
    mq135_ratio = np.full(n, 1.0) + mq135_drift
    flame_irradiance = np.zeros(n)

    if regime.name == "quiescent":
        pass

    elif regime.name == "occupancy":
        # People raise CO2 and humidity slightly; no particulates.
        temp += rng.uniform(0.2, 0.8)
        rh += rng.uniform(2.0, 6.0)
        mq135_ratio -= rng.uniform(0.03, 0.10) * progress
        mq2_ratio -= rng.uniform(0.0, 0.02) * progress

    elif regime.name == "cooking":
        # Frying: real particulates and VOCs, moderate temperature rise, but
        # no flame IR signature reaching the sensor and bounded severity.
        peak = rng.uniform(0.22, 0.42)
        shape = np.sin(math.pi * progress) ** 0.7
        mq2_ratio -= peak * shape
        mq135_ratio -= rng.uniform(0.15, 0.30) * shape
        temp += rng.uniform(2.0, 6.0) * shape
        rh += rng.uniform(3.0, 12.0) * shape
        # Stray IR from a hot pan / gas hob, well below a real flame.
        flame_irradiance += rng.uniform(0.0, 0.12) * shape

    elif regime.name == "steam":
        # Kettle / shower: humidity spike, minimal particulates. This is the
        # classic false positive for cheap smoke alarms.
        shape = np.sin(math.pi * progress) ** 0.6
        rh += rng.uniform(25.0, 45.0) * shape
        rh = np.clip(rh, 0.0, 99.0)
        temp += rng.uniform(1.0, 3.5) * shape
        # Condensation on the sensing element depresses Rs slightly.
        mq2_ratio -= rng.uniform(0.02, 0.09) * shape
        mq135_ratio -= rng.uniform(0.05, 0.14) * shape

    elif regime.name == "smoldering":
        # Pyrolysis without flame: heavy particulates and CO, slow thermal
        # ramp, no IR flame signature. This is the hardest class.
        ramp = progress**1.4
        mq2_ratio -= rng.uniform(0.45, 0.68) * ramp
        mq135_ratio -= rng.uniform(0.35, 0.60) * ramp
        temp += rng.uniform(4.0, 13.0) * ramp
        rh -= rng.uniform(2.0, 8.0) * ramp
        flame_irradiance += rng.uniform(0.0, 0.08) * ramp

    elif regime.name == "flaming":
        # Flashover: exponential heat release, strong 4.3 um IR emission,
        # saturating particulate response, sharp humidity collapse.
        ramp = np.expm1(3.2 * progress) / math.expm1(3.2)
        mq2_ratio -= rng.uniform(0.70, 0.88) * ramp
        mq135_ratio -= rng.uniform(0.60, 0.82) * ramp
        temp += rng.uniform(25.0, 120.0) * ramp
        rh -= rng.uniform(12.0, 30.0) * ramp
        flame_irradiance += rng.uniform(0.55, 0.98) * ramp

    elif regime.name == "post_fire":
        # Suppressed fire: residual smoke and heat decaying, flame gone.
        decay = np.exp(-2.5 * progress)
        mq2_ratio -= rng.uniform(0.30, 0.55) * decay
        mq135_ratio -= rng.uniform(0.25, 0.45) * decay
        temp += rng.uniform(8.0, 25.0) * decay
        rh += rng.uniform(5.0, 20.0) * decay  # suppression water
        flame_irradiance += rng.uniform(0.0, 0.05) * decay

    else:  # pragma: no cover - guarded by REGIMES definition
        raise ValueError(f"unknown regime {regime.name}")

    return {
        "temp": temp,
        "rh": np.clip(rh, 1.0, 100.0),
        "mq2_ratio": np.clip(mq2_ratio, 0.05, 3.0),
        "mq135_ratio": np.clip(mq135_ratio, 0.05, 3.0),
        "flame_irradiance": np.clip(flame_irradiance, 0.0, 1.0),
    }


def _apply_transducers(
    rng: np.random.Generator, raw: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """Turn physical quantities into what the microcontroller actually reads."""
    n = raw["temp"].size

    # DHT22: quantised to 0.1 and with datasheet-grade noise.
    temperature_c = np.round(
        raw["temp"] + rng.normal(0.0, DHT22_TEMP_NOISE_C / 3.0, n), 1
    )
    humidity_pct = np.clip(
        np.round(raw["rh"] + rng.normal(0.0, DHT22_RH_NOISE_PCT / 3.0, n), 1), 0.0, 100.0
    )

    # MQ sensors: ratio -> ppm -> analogue volts -> 12-bit ADC.
    # Clamped at the datasheet ceiling: a saturated sensor pins at full scale.
    smoke_ppm = np.minimum(
        mq_ratio_to_ppm(raw["mq2_ratio"], MQ2_A, MQ2_B), MQ2_MAX_PPM
    )
    air_quality_ppm = np.minimum(
        mq_ratio_to_ppm(raw["mq135_ratio"], MQ135_A, MQ135_B), MQ135_MAX_PPM
    )

    mq2_volts = adc_quantise(
        np.clip(3.3 * (1.0 - raw["mq2_ratio"] / 3.0), 0.0, 3.3)
        + rng.normal(0.0, 0.004, n)
    )
    mq135_volts = adc_quantise(
        np.clip(3.3 * (1.0 - raw["mq135_ratio"] / 3.0), 0.0, 3.3)
        + rng.normal(0.0, 0.004, n)
    )

    # IR flame sensor: analogue irradiance plus a comparator digital output.
    flame_analog = adc_quantise(
        np.clip(3.3 * raw["flame_irradiance"], 0.0, 3.3) + rng.normal(0.0, 0.006, n)
    )
    flame_detected = (raw["flame_irradiance"] > 0.35).astype(np.int8)

    return {
        "temperature_c": temperature_c,
        "humidity_pct": humidity_pct,
        "smoke_ppm": np.round(smoke_ppm, 3),
        "air_quality_ppm": np.round(air_quality_ppm, 3),
        "mq2_volts": np.round(mq2_volts, 4),
        "mq135_volts": np.round(mq135_volts, 4),
        "flame_analog_volts": np.round(flame_analog, 4),
        "flame_detected": flame_detected,
    }


# ---------------------------------------------------------------------------
# Label refinement
# ---------------------------------------------------------------------------


def _refine_labels(frame: pd.DataFrame) -> pd.Series:
    """Sharpen regime labels using the realised sensor values.

    A regime is a *scenario*, not a ground truth per sample: the first seconds
    of a flaming fire are not yet detectable, and a vigorous smoldering event
    can genuinely warrant a FIRE label. Labelling purely by regime would teach
    the model to predict the future, which it cannot do at inference time.
    We therefore derive the per-sample label from the realised physics.
    """
    label = pd.Series(frame["regime_label"].to_numpy(), index=frame.index, dtype=object)

    flame = frame["flame_analog_volts"] > 1.15
    hot = frame["temperature_c"] > 45.0
    very_hot = frame["temperature_c"] > 58.0
    heavy_smoke = frame["smoke_ppm"] > 900.0
    severe_smoke = frame["smoke_ppm"] > 2200.0
    bad_air = frame["air_quality_ppm"] > 300.0

    # A confirmed flame with corroborating heat OR smoke is unambiguous FIRE.
    fire_mask = (flame & (hot | heavy_smoke)) | (very_hot & heavy_smoke) | severe_smoke
    # Elevated but unconfirmed signatures are WARNING.
    warn_mask = ~fire_mask & (hot | heavy_smoke | bad_air | flame)

    label[warn_mask] = "WARNING"
    label[fire_mask] = "FIRE"

    # Early samples of a flaming regime that have not yet developed a
    # detectable signature stay at whatever the physics supports - they must
    # not be labelled FIRE, or the model learns to guess.
    undeveloped = (frame["regime"] == "flaming") & ~fire_mask & ~warn_mask
    label[undeveloped] = "SAFE"

    return label


# ---------------------------------------------------------------------------
# Feature engineering (shared with backend + firmware)
# ---------------------------------------------------------------------------

#: The exact feature order used by both models, the exported C tree and the
#: backend inference service. Changing this list is a breaking change.
FEATURE_COLUMNS: tuple[str, ...] = (
    "temperature_c",
    "humidity_pct",
    "smoke_ppm",
    "air_quality_ppm",
    "flame_analog_volts",
    "flame_detected",
    "temp_rate_c_per_min",
    "smoke_rate_ppm_per_min",
    "heat_index_c",
)


def compute_heat_index(temp_c: np.ndarray, rh_pct: np.ndarray) -> np.ndarray:
    """NOAA Rothfusz heat index, in Celsius.

    Included because perceived thermal load combines temperature and humidity,
    and a dry 50 C room is a very different hazard from a humid one.
    """
    t_f = temp_c * 9.0 / 5.0 + 32.0
    hi_f = (
        -42.379
        + 2.04901523 * t_f
        + 10.14333127 * rh_pct
        - 0.22475541 * t_f * rh_pct
        - 6.83783e-3 * t_f**2
        - 5.481717e-2 * rh_pct**2
        + 1.22874e-3 * t_f**2 * rh_pct
        + 8.5282e-4 * t_f * rh_pct**2
        - 1.99e-6 * t_f**2 * rh_pct**2
    )
    # Below 80 F the Rothfusz regression is invalid; fall back to a simple mean.
    simple_f = 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh_pct * 0.094)
    out_f = np.where(t_f >= 80.0, hi_f, simple_f)
    return (out_f - 32.0) * 5.0 / 9.0


def add_derived_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add rate-of-change and heat-index features, per device, in place-safe form.

    Rate of change is the single most discriminative signal for fire: a room
    that is hot is ambiguous, a room that is *getting hot fast* is not.
    """
    out = frame.copy()
    samples_per_min = 60.0 / SAMPLE_PERIOD_S

    grouped_temp = out.groupby("episode_id")["temperature_c"]
    grouped_smoke = out.groupby("episode_id")["smoke_ppm"]

    out["temp_rate_c_per_min"] = (
        grouped_temp.diff().fillna(0.0) * samples_per_min
    ).round(4)
    out["smoke_rate_ppm_per_min"] = (
        grouped_smoke.diff().fillna(0.0) * samples_per_min
    ).round(4)
    out["heat_index_c"] = compute_heat_index(
        out["temperature_c"].to_numpy(), out["humidity_pct"].to_numpy()
    ).round(3)
    return out


# ---------------------------------------------------------------------------
# Top-level generation
# ---------------------------------------------------------------------------


def generate(rows: int, seed: int) -> pd.DataFrame:
    """Generate at least ``rows`` labelled samples."""
    rng = np.random.default_rng(seed)
    weights = np.array([r.weight for r in REGIMES], dtype=np.float64)
    weights /= weights.sum()

    frames: list[pd.DataFrame] = []
    produced = 0
    episode_id = 0
    t_abs = float(rng.uniform(0.0, DIURNAL_PERIOD_S))

    while produced < rows:
        regime = REGIMES[int(rng.choice(len(REGIMES), p=weights))]
        duration = float(rng.uniform(regime.min_duration_s, regime.max_duration_s))
        n = max(8, int(duration / SAMPLE_PERIOD_S))

        times = t_abs + np.arange(n) * SAMPLE_PERIOD_S
        raw = _simulate_regime(rng, regime, n, times)
        readings = _apply_transducers(rng, raw)

        segment = pd.DataFrame(readings)
        segment["timestamp_s"] = np.round(times, 2)
        segment["episode_id"] = episode_id
        segment["regime"] = regime.name
        segment["regime_label"] = regime.label
        frames.append(segment)

        produced += n
        episode_id += 1
        t_abs = float(times[-1] + SAMPLE_PERIOD_S)

    data = pd.concat(frames, ignore_index=True)
    data = add_derived_features(data)
    data["label"] = _refine_labels(data)
    data["label_int"] = data["label"].map(LABEL_TO_INT).astype(np.int8)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=24000)
    parser.add_argument("--seed", type=int, default=20260726)
    parser.add_argument("--out", type=Path, default=Path("ml/dataset.csv"))
    args = parser.parse_args()

    data = generate(args.rows, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(args.out, index=False)

    counts = data["label"].value_counts()
    print(f"wrote {len(data)} rows -> {args.out}")
    for name in LABELS:
        count = int(counts.get(name, 0))
        print(f"  {name:<8} {count:>6}  ({count / len(data) * 100:5.2f}%)")


if __name__ == "__main__":
    main()
