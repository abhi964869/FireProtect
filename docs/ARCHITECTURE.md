# FireProtect — Architecture

## System overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│ EDGE                                                                    │
│                                                                         │
│  MQ-2 ──┐                                                               │
│  MQ-135 ├──► ESP32 ──► feature vector ──► decision tree (C) ──► local    │
│  DHT22 ─┤       │                              │                alarm   │
│  Flame ─┘       │                              │                        │
│                 │                    SAFE / WARNING / FIRE               │
│                 ▼                                                        │
│          ┌──────────────┐                                                │
│          │ MQTT publish │  fireprotect/<device_id>/telemetry   (QoS 1)    │
│          │ ThingSpeak   │  ring buffer, drains on reconnect              │
│          └──────────────┘                                                │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │
                     ┌─────▼──────┐
                     │ Mosquitto  │  :1883
                     └─────┬──────┘
                           │  fireprotect/+/telemetry
┌──────────────────────────▼──────────────────────────────────────────────┐
│ BACKEND (FastAPI)                                                       │
│                                                                         │
│  paho thread ──► TelemetryIn validation ──► IngestService               │
│                       (reject NaN,              │                       │
│                        out-of-range)            │                       │
│                                                 ▼                       │
│                                        derive rate features             │
│                                                 │                       │
│                                        random forest (n_jobs=1)         │
│                                                 │                       │
│                    ┌────────────────────────────┼──────────────┐        │
│                    ▼                            ▼              ▼        │
│                 SQLite                    alert engine   ThingSpeak     │
│               (WAL mode)                  (debounced)      mirror       │
│                    │                            │                       │
│                    └────────────┬───────────────┘                       │
│                                 ▼                                       │
│                        ConnectionHub.broadcast_threadsafe               │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │  WebSocket /ws
┌─────────────────────────────────▼───────────────────────────────────────┐
│ FRONTEND (React + Vite)                                                 │
│  useLiveData: REST snapshot + WS deltas, reconnect w/ jittered backoff   │
│  StatusHero · DeviceGrid · SensorCharts · RiskGauge · Alerts · Thresholds│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Components

### `firmware/` — ESP32

| File | Role |
|---|---|
| `src/main.cpp` | Main loop, Wi-Fi/MQTT lifecycle, publish |
| `include/fire_model.h` | **Generated** decision tree (do not hand-edit) |
| `include/sensors.h` | ADC → volts → Rs → ppm conversion, validation |
| `include/thingspeak_buffer.h` | Fixed-size RAM ring buffer with retry |
| `diagram.json` | Wokwi wiring for in-browser simulation |

The loop never blocks on the network. Wi-Fi and MQTT each have their own
exponential backoff with a next-attempt timestamp, so a downed AP degrades to
"sampling and classifying locally, not publishing" rather than stalling.

### `ml/` — models

`generate_dataset.py` synthesises episodes of sensor behaviour; `train.py`
fits both models, exports the tree to C, and **gates the build**: it exits
non-zero if FIRE recall drops below 0.98, if C/Python parity is not exact, or
if the generated header exceeds 20 KB.

### `backend/` — FastAPI

| Module | Role |
|---|---|
| `config.py` | Pydantic Settings, env-overridable |
| `db.py` | SQLAlchemy models, session scope, SQLite pragmas |
| `schemas.py` | Pydantic contracts, range + finiteness validation |
| `inference.py` | Random forest wrapper, risk score, threshold fallback |
| `ingest.py` | The one path telemetry takes to become state |
| `mqtt_client.py` | paho subscriber, defensive callbacks |
| `thingspeak.py` | Cloud mirror, disk-backed buffer, bounded retry |
| `ws.py` | WebSocket hub, thread → event-loop bridge |
| `main.py` | Routes, lifespan, offline sweeper |

### `frontend/` — React dashboard

`useLiveData` is the single source of live state: a REST snapshot on mount,
then WebSocket deltas. All components are pure renderers of that state.

---

## Design decisions

### Two models, not one

The device tree exists so a node still alarms with the network down. The server
forest exists because it can be much larger and more accurate. They share one
feature vector so their verdicts are comparable, and both are stored on every
reading (`device_status` vs `server_status`) — a persistent disagreement is a
useful signal that a device needs recalibration.

The server model is authoritative for alerting. A device could be compromised
or miscalibrated; the backend should not take its word for a life-safety state.

### The topic is authoritative for device identity

`mqtt_client.handle_message` overwrites any `device_id` in the payload with the
one from the topic. Otherwise any device on the broker could publish readings
attributed to another.

### Rate-of-change features are load-bearing

A room that *is* hot is ambiguous. A room that is *getting hot fast* is not.
`temp_rate_c_per_min` and `smoke_rate_ppm_per_min` require the previous sample,
so `IngestService` keeps per-device state in memory rather than querying the
database on every reading. The state is per device — a new device never
inherits another's history.

### Alerts are debounced, but barely

A single anomalous reading must not page anyone, so FIRE requires 2 consecutive
classifications and WARNING requires 3. At the 2 s publish rate that is a 4 s
delay for fire — small against the time constant of a real fire, and enough to
reject single-sample noise. Two consecutive SAFE readings resolve an open alert.

### `n_jobs = 1` at serving time

The forest is fitted with `n_jobs=-1`, and joblib persists that. At serving
time we classify one row at a time, where the worker-pool setup costs ~85 ms
against ~1 ms of real work. `inference.py` forces `n_jobs = 1` after load — a
~50x speedup measured on this workload.

### SQLite in WAL mode

The MQTT ingest thread writes continuously while HTTP handlers read. SQLite's
default rollback journal serialises readers against the writer and produces
"database is locked" under that pattern. WAL plus a 15 s busy timeout removes
it. If you outgrow SQLite, `DATABASE_URL` takes any SQLAlchemy URL.

### The WebSocket hub bridges a thread boundary

paho callbacks run on their own thread with no event loop; `send_text` must run
on the FastAPI loop. `ConnectionHub.bind_loop` captures the loop at startup and
`broadcast_threadsafe` schedules onto it. Broadcast failures are swallowed per
client and the dead client pruned — a closed browser tab must not stop the
fan-out to everyone else.

### ThingSpeak never blocks ingest

The mirror is called last in `_process`, inside a `try/except`, and sends at
most one buffered entry per reading. A cloud outage costs buffer memory, not
ingest latency. Permanent 4xx responses drop the entry rather than retrying
forever; 429 and 5xx are retried with exponential backoff.

### Failure modes, and what happens

| Failure | Behaviour |
|---|---|
| Broker down at boot | API still starts; `/api/health` reports `mqtt_connected: false`; paho retries |
| Broker drops mid-run | paho reconnects with backoff; readings during the gap are lost (not buffered by design — the DB is the record) |
| Malformed MQTT payload | Rejected, counted in `stats.rejected`, ingest continues |
| Sensor returns NaN | Rejected at the schema; firmware also rejects locally and holds the last status |
| Model file missing | Threshold fallback rules; `/api/health` reports `model_loaded: false` |
| Model file corrupt | Same fallback; exception logged, not raised |
| ThingSpeak down | Buffered to disk, drained on recovery, oldest dropped when full |
| Device goes silent | Offline sweeper flips `online: false` after 30 s and broadcasts |
| Browser tab closed | Client pruned from the hub on the next failed send |
| WebSocket drops | Client reconnects with jittered backoff and re-fetches the REST snapshot |

---

## Data model

```
devices (id PK)
   │ 1:N
   ├──► readings (device_id FK, recorded_at idx)   ← time series
   └──► alerts   (device_id FK, triggered_at idx)  ← append-only

thresholds (key PK)                                ← singleton config rows
```

Readings are never mutated. Alerts are append-only; resolution sets
`resolved_at` rather than deleting. `DELETE /api/readings` prunes beyond the
retention window.

---

## Security posture

As shipped this is a **trusted-LAN** system:

- The MQTT broker allows anonymous connections.
- The REST API and WebSocket are unauthenticated.
- CORS is restricted to localhost dev origins by default.

Before exposing any of it: enable Mosquitto auth (`mosquitto/config/mosquitto.conf`
documents the steps), put the API behind a reverse proxy with TLS and
authentication, and set `CORS_ORIGINS` explicitly.

No secrets are committed. `firmware/secrets.ini` and `.env` are git-ignored and
both ship as `.example` files.
