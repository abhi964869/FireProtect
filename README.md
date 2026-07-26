# FireProtect

An IoT smart fire detection system. ESP32 nodes read four sensors, classify
fire risk on-device with an exported decision tree, and publish over MQTT. A
FastAPI backend re-classifies each reading with a random forest, persists it,
raises alerts, and streams everything live to a React dashboard.

**No hardware required.** A virtual device reproduces the full sensor physics,
so the entire system runs and demonstrates end to end on a laptop.

---

## Quick start

**No Docker, no Node, one terminal:**

```bash
pip install -r backend/requirements-dev.txt
python run_local.py
```

Your browser opens on **http://localhost:8000** with a live device, live
charts and a risk gauge. That one command starts an MQTT broker, the backend
(which also serves the dashboard), and a simulated ESP32.

To watch a fire develop:

```bash
python run_local.py --scenario flashover
```

The dashboard escalates SAFE → WARNING → FIRE and raises an alert in about
15 seconds. Ctrl-C stops everything.

| | |
|---|---|
| Dashboard | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |
| Health | http://localhost:8000/api/health |

Useful flags: `--scenario`, `--port`, `--no-simulator`, `--no-browser`,
`--no-broker` (posts over HTTP instead of MQTT), `--speed`.

**Deployed demo:** dashboard on Vercel, backend on Render — see
[docs/DEPLOY.md](docs/DEPLOY.md). Vercel cannot host the backend (no
WebSockets, no background threads, ephemeral filesystem), so the two are split.

<details>
<summary>Docker instead</summary>

```bash
docker compose --profile demo up --build     # → http://localhost:5173
SIM_SCENARIO=flashover docker compose --profile demo up --build
```

</details>

---

## Running without Docker

<details>
<summary>Backend + simulator on your machine</summary>

```bash
# 1. Install Python deps
pip install -r backend/requirements-dev.txt

# 2. Train the models (writes ml/*.joblib, ml/METRICS.md and the firmware header)
python ml/generate_dataset.py --rows 24000 --out ml/dataset.csv
python ml/train.py

# 3. Start the backend
cd backend && uvicorn app.main:app --reload --port 8000
```

The backend runs without a broker — it logs a warning and keeps retrying. To
push data in without MQTT at all, point the simulator at the REST endpoint:

```bash
python simulator/virtual_device.py --transport http --scenario flashover
```

</details>

<details>
<summary>Dashboard in dev mode</summary>

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173, proxies /api and /ws to :8000
```

</details>

---

## What's in the box

```
fireprotect/
├── firmware/     ESP32 PlatformIO project, generated model header, Wokwi diagram
├── ml/           Dataset generator, training, exported models, metrics
├── backend/      FastAPI + MQTT subscriber + SQLite + WebSocket + tests
├── frontend/     React dashboard + generated assets + tests
├── simulator/    Virtual ESP32 (normal / smoldering / flashover scenarios)
├── mosquitto/    Broker config
├── docs/         ARCHITECTURE, API, HARDWARE, SETUP
├── design.md     UI/UX source of truth
├── PROGRESS.md   Build log, phase by phase
└── BUGS.md       Every defect found and how it was fixed
```

### The pipeline

```
ESP32 / simulator
   │  MQTT  fireprotect/<device_id>/telemetry
   ▼
Mosquitto ──► backend subscriber ──► validation ──► feature derivation
                                                          │
                                      random forest ◄──────┘
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
                 SQLite               alert engine            WebSocket ──► dashboard
                    │                       │
                    └──► ThingSpeak mirror (buffered, retried)
```

Both models share one nine-feature vector. The on-device decision tree gives an
immediate local alarm even if the network is down; the server-side random
forest is the authority for alerting.

---

## Model performance

Held-out test set, split **by episode** so no autocorrelated neighbour leaks
across the boundary. Full detail in [`ml/METRICS.md`](ml/METRICS.md); every data
assumption is documented in [`ml/DATA.md`](ml/DATA.md).

| Model | Accuracy | Macro F1 | **FIRE recall** | FIRE precision |
|---|---|---|---|---|
| Random Forest (server) | 0.9121 | 0.9185 | **0.9970** | 1.0000 |
| Decision Tree (device) | 0.8399 | 0.8654 | **1.0000** | 1.0000 |

`FIRE` recall is the primary metric — false negatives are unacceptable, false
positives are tolerable — and both models clear the 0.98 target, with no false
FIRE alarms on the held-out set.

Overall accuracy is deliberately *not* the headline. Both models are fitted
with class weights `{SAFE: 1, WARNING: 2.5, FIRE: 8}`, which trades
SAFE/WARNING accuracy for FIRE sensitivity on purpose. Most of the residual
error is SAFE readings pushed into WARNING — the direction a fire detector
should err in. The decision tree is additionally depth-limited to 9 to fit the
ESP32 flash budget, which costs it a further ~7 points of accuracy against the
unconstrained forest.

The exported C tree agrees with the Python model on **100%** of held-out
samples (6,871 compared, 0 mismatches; 17,704 bytes, under the 20 KB budget).

---

## Testing

```bash
# Python: backend, ML, simulator scenarios, and a real-broker E2E test
pytest

# Linting and types
ruff check backend ml simulator
mypy backend/app ml/generate_dataset.py ml/export_tree.py

# Frontend
cd frontend && npm run lint && npm run typecheck && npm test && npm run build

# Firmware logic, no ESP32 toolchain needed
cd firmware && ./check_host_build.sh
```

`backend/tests/test_e2e_mqtt.py` spins up a real in-process MQTT broker and
drives the whole chain — simulator → MQTT → inference → SQLite → WebSocket —
including the flashover and normal scenarios.

### Live-stack smoke test

To prove the *running system* works — real broker process, real uvicorn server,
real sockets — rather than an in-process import:

```bash
python scripts/smoke_test.py --scenario flashover   # expects a FIRE alert
python scripts/smoke_test.py --scenario normal      # expects no FIRE alert
python scripts/smoke_test.py --keep-running         # leaves it up to poke at
```

It boots the stack on free ports, asserts every REST endpoint's response shape,
opens a live WebSocket, checks alerting, and tears everything down. Exit code 0
means the stack works.

---

## Hardware

Wiring diagram, pin table and BOM are in [`docs/HARDWARE.md`](docs/HARDWARE.md).

To build and flash:

```bash
cd firmware
cp secrets.ini.example secrets.ini   # edit Wi-Fi / MQTT / device id
pio run -t upload
pio device monitor
```

To run it in a browser with no hardware, use the Wokwi VS Code extension with
`firmware/diagram.json` (`pio run -e wokwi`, then *Wokwi: Start Simulator*).

---

## Documentation

| Document | Contents |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Install, configure, ThingSpeak, troubleshooting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, design decisions |
| [docs/API.md](docs/API.md) | Every REST endpoint and the WebSocket protocol |
| [docs/HARDWARE.md](docs/HARDWARE.md) | Wiring, pin table, BOM, calibration |
| [ml/DATA.md](ml/DATA.md) | Dataset generation and every assumption behind it |
| [ml/METRICS.md](ml/METRICS.md) | Full metrics, confusion matrices, parity check |
| [frontend/public/assets/ASSETS.md](frontend/public/assets/ASSETS.md) | Every generated image and its contrast check |
| [PROGRESS.md](PROGRESS.md) | Build log and verification status |
| [BUGS.md](BUGS.md) | Defects found, root causes, fixes |

---

## Limitations

Read these before trusting it with anything.

- **Not a certified life-safety device.** No UL 217 / EN 54 validation. Do not
  rely on it in place of a listed smoke alarm.
- **The models are trained on synthetic data.** The physics is documented and
  plausible, but it is not real fire-test data. See `ml/DATA.md` §7.
- **The label definition is an assumption.** Metrics are conditional on the
  threshold rules in `ml/DATA.md` §5 and cannot exceed their quality.
- **No sensor-fault modelling.** The firmware and backend reject NaN and
  out-of-range readings defensively, but the models have never seen a
  poisoned, drifted or disconnected sensor.
- **No authentication.** The API and MQTT broker are unauthenticated and
  intended for a trusted LAN. Do not expose them to the internet as shipped.
- **MQ sensors need burn-in.** 24–48 h of powered burn-in and per-unit R₀
  calibration; see `docs/HARDWARE.md`.
#   F i r e P r o t e c t  
 