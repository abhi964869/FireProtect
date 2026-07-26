# FireProtect — Synthetic Dataset

No public dataset combines MQ-2, MQ-135, DHT22 and IR-flame readings with
per-sample fire labels, and this build has no network dependency at runtime, so
`ml/generate_dataset.py` synthesises one from documented sensor physics. This
file records **every assumption** so the numbers in `METRICS.md` can be judged
against them.

Regenerate with:

```bash
python ml/generate_dataset.py --rows 24000 --seed 20260726 --out ml/dataset.csv
```

Deterministic given `--seed`. Current committed dataset: **24,186 rows**
(SAFE 63.99%, WARNING 30.62%, FIRE 5.39%).

---

## 1. Structure

The dataset is not i.i.d. rows — it is a concatenation of **episodes**. Each
episode is a contiguous stretch of one *regime* sampled at 2 s intervals
(`SAMPLE_PERIOD_S`), matching the firmware's publish rate. Episodes carry an
`episode_id`, which is what train/test splitting groups on.

| Regime | Nominal label | Duration | Weight | What it represents |
|---|---|---|---|---|
| `quiescent` | SAFE | 120–900 s | 0.34 | Empty room, nothing happening |
| `occupancy` | SAFE | 60–420 s | 0.12 | People present — CO₂ and RH rise slightly |
| `cooking` | WARNING | 90–480 s | 0.14 | Frying: real particulates + VOCs, no flame at sensor |
| `steam` | SAFE | 60–300 s | 0.10 | Kettle/shower: large RH spike, no particulates |
| `smoldering` | WARNING | 120–600 s | 0.12 | Pyrolysis without flame — slow ramp, heavy CO |
| `flaming` | FIRE | 60–360 s | 0.12 | Flashover: exponential heat, strong IR |
| `post_fire` | WARNING | 60–300 s | 0.06 | Suppressed fire, residual smoke and heat decaying |

**Assumption:** the nuisance regimes (`cooking`, `steam`) are deliberately
over-represented relative to a real deployment. Real buildings are ~99%
quiescent. Over-sampling nuisances forces the models to learn to *reject* them
rather than treating any particulate rise as fire. This inflates apparent
false-positive difficulty and is intentional.

---

## 2. Sensor models

### MQ-2 (smoke / combustible gas) and MQ-135 (air quality)

Both are chemiresistive. The datasheet gives a log-log relationship between the
resistance ratio Rs/R₀ and concentration, fitted as `ppm = a · (Rs/R₀)^b`:

| Sensor | a | b | R₀ (clean air) |
|---|---|---|---|
| MQ-2 (LPG/smoke) | 574.25 | −2.222 | 9.83 kΩ |
| MQ-135 (CO₂-equiv) | 116.602 | −2.769 | 76.63 kΩ |

**Assumptions:**

- A *single* curve is used per sensor. Real MQ sensors have a different curve
  per target gas and no selectivity between them; a deployed unit cannot tell
  LPG from smoke. Modelling one blended curve reflects what the firmware can
  actually observe.
- R₀ is treated as nominal and fixed. Real R₀ drifts with sensor age and
  requires periodic recalibration; this is captured as baseline drift (below)
  rather than as an explicit ageing term.
- Humidity cross-sensitivity is modelled only as a small Rs depression during
  `steam` (condensation on the sensing element). Real MQ humidity
  cross-sensitivity is larger and non-linear.
- No warm-up transient. Real MQ sensors need 24–48 h burn-in and ~60 s of
  preheat from cold; the dataset assumes a sensor already at thermal
  equilibrium.

### Baseline drift

Chemiresistive baselines wander; white noise would be wrong. Drift is an
**Ornstein–Uhlenbeck process** (θ = 0.02, σ = 0.010 for MQ-2, σ = 0.008 for
MQ-135) — mean-reverting and time-correlated. This is the single most important
realism choice in the generator: it is what stops the models from keying on an
implausibly stable baseline.

### DHT22 (temperature + humidity)

- Gaussian noise at σ = datasheet accuracy / 3 (±0.5 °C, ±2 % RH → σ = 0.167 °C,
  0.667 % RH), quantised to 0.1 resolution.
- **Assumption:** the DHT22's ~2 s minimum sampling interval is exactly the
  publish rate, so no interpolation is modelled.
- **Assumption:** the sensor is rated to 80 °C. In `flaming` episodes the
  modelled air temperature exceeds this; readings are *not* clipped, because
  the firmware's job is to alarm long before that point and clipping would hide
  the ramp. Real hardware would saturate or fail — see Limitations.

### IR flame sensor

- Modelled as normalised irradiance in [0, 1] scaled to 0–3.3 V analogue, plus a
  comparator digital output that trips above 0.35 normalised.
- **Assumption:** the sensor responds to the 4.3 µm CO₂ emission band, so hot
  surfaces without combustion (a `cooking` pan) produce only weak stray IR
  (≤ 0.12) while open flame produces 0.55–0.98.
- **Assumption:** no sunlight interference. Direct sun and incandescent lamps
  are real false-positive sources for IR flame sensors and are **not** modelled.

### ADC

All analogue channels pass through `adc_quantise`: clipped to the ESP32's
trustworthy 0.15–3.1 V window, then 12-bit quantised against a 3.3 V reference.
The ESP32 SAR ADC is markedly non-linear near both rails; clipping to the usable
window models how the firmware treats it, rather than modelling the full
non-linearity curve.

---

## 3. Ambient conditions

- Indoor baseline 22 °C / 45 % RH.
- Diurnal temperature swing: ±3 °C sinusoid over 24 h.
- RH is anti-correlated with the diurnal term at −0.6 × the temperature
  deviation, approximating constant absolute humidity.
- **Assumption:** one room, one climate. No seasonal variation, no HVAC cycling,
  no draughts.

---

## 4. Features

Nine features, in the exact order used by both models, the exported C tree and
the backend (`FEATURE_COLUMNS`):

| # | Feature | Source |
|---|---|---|
| 0 | `temperature_c` | DHT22 |
| 1 | `humidity_pct` | DHT22 |
| 2 | `smoke_ppm` | MQ-2 via curve |
| 3 | `air_quality_ppm` | MQ-135 via curve |
| 4 | `flame_analog_volts` | IR sensor analogue |
| 5 | `flame_detected` | IR comparator (0/1) |
| 6 | `temp_rate_c_per_min` | Δ`temperature_c` × 30, within episode |
| 7 | `smoke_rate_ppm_per_min` | Δ`smoke_ppm` × 30, within episode |
| 8 | `heat_index_c` | NOAA Rothfusz regression |

**Rate-of-change features are load-bearing.** A room that *is* hot is ambiguous
(it could be a sunny afternoon); a room that is *getting hot fast* is not. Rates
are computed per `episode_id` so no gradient is ever calculated across an
episode boundary — the first sample of each episode gets rate 0.

`heat_index_c` uses the Rothfusz regression above 80 °F and the simple average
form below it, matching NOAA practice. It is included because a dry 50 °C room
and a humid 50 °C room are different hazards.

---

## 5. Labelling

Labels are **not** taken directly from the regime. A regime is a scenario, not
per-sample ground truth: the first seconds of a flaming fire are not yet
detectable from any sensor, and labelling them FIRE would train the model to
predict the future — which it cannot do at inference time, so it would learn
noise instead.

`_refine_labels` derives the label from realised sensor values:

```
flame        = flame_analog_volts > 1.15 V
hot          = temperature_c      > 45 °C
very_hot     = temperature_c      > 58 °C
heavy_smoke  = smoke_ppm          > 900 ppm
severe_smoke = smoke_ppm          > 2200 ppm
bad_air      = air_quality_ppm    > 300 ppm

FIRE    ⟸ (flame ∧ (hot ∨ heavy_smoke)) ∨ (very_hot ∧ heavy_smoke) ∨ severe_smoke
WARNING ⟸ ¬FIRE ∧ (hot ∨ heavy_smoke ∨ bad_air ∨ flame)
SAFE    ⟸ otherwise
```

**Rationale for the corroboration rule:** FIRE requires *two independent*
signatures (flame + heat, flame + smoke, or heat + smoke), except at
`severe_smoke` where the smoke reading alone is unambiguous. A single elevated
channel yields WARNING. This is what keeps `cooking` and `steam` out of the FIRE
class — a hot pan raises temperature but not flame IR, and steam raises humidity
but not particulates.

Undeveloped early `flaming` samples that trip neither mask are explicitly
relabelled SAFE.

**This is the largest assumption in the dataset.** The thresholds encode a
particular definition of "fire", and every reported metric is conditional on it.
They are consistent with common smoke-alarm trip points but are not calibrated
against certified fire-test data (UL 217 / EN 54 test fires).

---

## 6. Train/test split

`GroupShuffleSplit` on `episode_id`, 75/25. **Never split by row.** Consecutive
2-second samples within an episode are strongly autocorrelated; a random row
split leaks near-identical neighbours across the boundary and inflates every
score — often to a spurious ~0.999 accuracy. Grouping by episode keeps whole
events on one side.

---

## 7. Known limitations

1. **Synthetic.** Real fire signatures are messier. Nothing here substitutes for
   validation against certified fire tests.
2. **Threshold-derived labels** — the models can only be as good as the labelling
   rule in §5, and cannot outperform it by definition.
3. **No sensor faults.** Disconnection, poisoning, drift beyond OU range and
   end-of-life are not in the data. The firmware and backend handle NaN and
   out-of-range values defensively, but the *models* have never seen a fault.
4. **No sunlight / IR interference**, the main real false-positive source for IR
   flame sensors.
5. **Single room, single sensor placement.** No modelling of distance to fire,
   ceiling jet dynamics, or ventilation.
6. **DHT22 over-range** readings in flaming episodes exceed the sensor's 80 °C
   rating and would not be readable on real hardware.
7. **No adversarial or tampering scenarios.**
