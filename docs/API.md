# FireProtect — API Reference

Base URL: `http://localhost:8000`
Interactive docs: `/docs` (Swagger) · `/redoc` · machine-readable: `/openapi.json`

No authentication — see the security note in `ARCHITECTURE.md`.

---

## Conventions

- All timestamps are ISO 8601 with a UTC offset.
- Errors return `{"detail": "..."}`. Validation failures return `422` with
  Pydantic's structured error list.
- `FireStatus` is one of `"SAFE" | "WARNING" | "FIRE"`.

---

## System

### `GET /api/health`

```json
{
  "status": "ok",
  "database": true,
  "mqtt_connected": true,
  "model_loaded": true,
  "thingspeak_pending": 0,
  "version": "1.0.0"
}
```

`status` is `"degraded"` when the database is unreachable. Note that
`mqtt_connected: false` and `model_loaded: false` do **not** degrade the
status — the service is intentionally still useful in both cases, and the
individual flags tell you which capability is missing.

### `GET /api/stats`

```json
{
  "total_devices": 3,
  "online_devices": 2,
  "total_readings": 18422,
  "open_alerts": 1,
  "alerts_24h": 4,
  "current_status": "WARNING",
  "max_fire_probability": 0.41
}
```

`current_status` is the worst status across the 50 most recent readings.

---

## Devices

### `GET /api/devices`

Array of:

```json
{
  "id": "esp32-sim-01",
  "name": "esp32-sim-01",
  "location": "Kitchen",
  "firmware_version": "1.0.0",
  "first_seen": "2026-07-26T09:14:02.117Z",
  "last_seen": "2026-07-26T09:41:55.882Z",
  "online": true
}
```

### `GET /api/devices/{device_id}`

The device plus `latest_reading` (a full reading object, or `null`) and
`open_alerts` (integer). `404` if unknown.

### `GET /api/devices/{device_id}/readings`

| Param | Type | Default | Range |
|---|---|---|---|
| `limit` | int | 200 | 1–5000 |

Newest first. `404` if the device is unknown.

---

## Readings

### `GET /api/readings`

| Param | Type | Default | Range |
|---|---|---|---|
| `device_id` | string | — | — |
| `limit` | int | 200 | 1–5000 |
| `since_minutes` | int | — | 1–10080 |

Newest first. Reading shape:

```json
{
  "id": 18422,
  "device_id": "esp32-sim-01",
  "recorded_at": "2026-07-26T09:41:55.882Z",
  "temperature_c": 23.1,
  "humidity_pct": 44.2,
  "smoke_ppm": 612.4,
  "air_quality_ppm": 118.7,
  "flame_analog_volts": 0.15,
  "flame_detected": 0,
  "temp_rate_c_per_min": 0.6,
  "smoke_rate_ppm_per_min": 12.4,
  "heat_index_c": 22.9,
  "device_status": "SAFE",
  "server_status": "SAFE",
  "fire_probability": 0.004,
  "risk_score": 1.2
}
```

`device_status` is the on-device tree's verdict as reported by the node;
`server_status` is the backend forest's, and is what drives alerting.

### `POST /api/telemetry`

Ingest a reading over HTTP instead of MQTT. Same pipeline, same validation —
useful for testing and for the simulator's `--transport http` mode.

```bash
curl -X POST http://localhost:8000/api/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"esp32-01","temperature_c":22.4,"humidity_pct":45.2,
       "smoke_ppm":560,"air_quality_ppm":110,"flame_analog_volts":0.15,
       "flame_detected":0,"device_status":"SAFE","location":"Kitchen"}'
```

Returns the created reading. Accepted ranges (anything outside → `422`):

| Field | Range |
|---|---|
| `temperature_c` | −40 … 300 |
| `humidity_pct` | 0 … 100 |
| `smoke_ppm` | 0 … 100 000 |
| `air_quality_ppm` | 0 … 100 000 |
| `flame_analog_volts` | 0 … 3.3 |
| `flame_detected` | 0 or 1 |

`NaN` and `Infinity` are rejected on every float field — a failed DHT22 read
produces `NaN` on real hardware, so this path matters in production.

### `DELETE /api/readings`

| Param | Type | Default |
|---|---|---|
| `older_than_days` | int | `RETENTION_DAYS` (30) |

Returns `{"deleted": 1204, "older_than_days": 30}`.

---

## Alerts

### `GET /api/alerts`

| Param | Type | Default | Range |
|---|---|---|---|
| `device_id` | string | — | — |
| `only_open` | bool | false | — |
| `limit` | int | 100 | 1–1000 |

```json
{
  "id": 42,
  "device_id": "esp32-sim-01",
  "severity": "FIRE",
  "message": "Fire detected: temperature 88.0 C, heavy smoke 4200 ppm, flame detected (P(fire)=0.94)",
  "triggered_at": "2026-07-26T09:41:55.882Z",
  "resolved_at": null,
  "acknowledged": false,
  "temperature_c": 88.0,
  "smoke_ppm": 4200.0,
  "fire_probability": 0.94
}
```

### `POST /api/alerts/{id}/acknowledge`
### `POST /api/alerts/{id}/resolve`

Both return the updated alert; `404` if unknown. Acknowledging records that a
human saw it; resolving closes it. Alerts also auto-resolve after two
consecutive SAFE readings.

---

## Thresholds

### `GET /api/thresholds`

```json
[{ "key": "temperature_fire_c", "value": 58.0, "updated_at": "..." }]
```

Seeded keys: `temperature_warning_c`, `temperature_fire_c`,
`smoke_warning_ppm`, `smoke_fire_ppm`, `air_quality_warning_ppm`,
`flame_volts`, `fire_probability_alert`.

### `PUT /api/thresholds`

```json
{ "thresholds": { "temperature_fire_c": 65.0 } }
```

Partial updates only touch the keys you send. An unknown key returns `422`
listing it, and **no** values are written. Negative and non-finite values are
rejected.

> These shape alert *wording* and the fallback rules used when the model is
> unavailable. They do not retrain or reweight the random forest.

---

## Model

### `POST /api/predict`

Ad-hoc classification, bypassing persistence.

```json
{
  "status": "FIRE",
  "fire_probability": 0.94,
  "risk_score": 96.5,
  "probabilities": { "SAFE": 0.01, "WARNING": 0.05, "FIRE": 0.94 }
}
```

`temp_rate_c_per_min` and `smoke_rate_ppm_per_min` default to 0; `heat_index_c`
is computed for you. Probabilities sum to 1.

---

## WebSocket

### `ws://localhost:8000/ws`

Every frame:

```json
{ "type": "reading" | "alert" | "device" | "stats" | "hello", "payload": { } }
```

On connect the server sends `{"type": "hello", "payload": {"version": "1.0.0", "protocol": 1}}`.
Thereafter frames are pushed as events occur. The client is not expected to
send anything; the server reads only to detect disconnects promptly.

| Type | Payload | Emitted when |
|---|---|---|
| `hello` | `{version, protocol}` | On connect |
| `reading` | Reading object | Every accepted telemetry sample |
| `alert` | Alert object | Alert raised **or** resolved |
| `device` | Device object | New device, or online/offline transition |
| `stats` | Stats object | Reserved; not currently emitted |

**Reconnection is the client's job.** Frames published while disconnected are
not replayed, so after reconnecting you must re-fetch the REST snapshot —
`useLiveData` in the dashboard does exactly this.

```js
const ws = new WebSocket('ws://localhost:8000/ws');
ws.onmessage = (e) => {
  const { type, payload } = JSON.parse(e.data);
  if (type === 'reading') console.log(payload.server_status);
};
```

---

## MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `fireprotect/<device_id>/telemetry` | device → backend | Same JSON as `POST /api/telemetry` |
| `fireprotect/<device_id>/alert` | device → broker | Telemetry snapshot on local FIRE transition (informational) |
| `fireprotect/<device_id>/status` | device → broker | `"online"` / `"offline"` (retained, MQTT last will) |

The backend subscribes to all three at QoS 1 but only acts on `telemetry` —
the server-side model is the authority for alerts, not the device.

The `<device_id>` in the topic is authoritative; any `device_id` in the body is
overwritten with it.
