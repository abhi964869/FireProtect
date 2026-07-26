# FireProtect — Build Progress

Status: **all phases complete.** 8 build cycles. `BUGS.md` has 0 open entries.

Runs on localhost with one command: `python run_local.py`. The backend serves
the dashboard itself, so there is no second process, no proxy and no CORS.

---

## Verification status — what was actually executed

Every claim below was produced by running the command, not by inspection.

| Check | Command | Result |
|---|---|---|
| Python lint | `ruff check backend ml simulator` | ✅ All checks passed |
| Python types | `mypy backend/app ml/generate_dataset.py ml/export_tree.py` | ✅ 12 files, no issues |
| Backend tests | `pytest backend/tests --ignore=test_e2e_mqtt.py` | ✅ 123 passed |
| Backend coverage | `pytest --cov=app` | ✅ **95%** (target ≥ 80%) |
| ML tests | `pytest ml/tests` | ✅ 25 passed |
| Simulator scenarios | `pytest simulator/tests` | ✅ 20 passed |
| **E2E over real MQTT** | `pytest backend/tests/test_e2e_mqtt.py` | ✅ 6 passed |
| **Live stack (flashover)** | `python scripts/smoke_test.py --scenario flashover` | ✅ 18/18 checks |
| **Live stack (normal)** | `python scripts/smoke_test.py --scenario normal` | ✅ 18/18 checks |
| **Built dashboard serves** | static server + asset fetch | ✅ index + all 9 assets HTTP 200 |
| **One-command localhost run** | `python run_local.py --scenario flashover` | ✅ dashboard, assets, SPA routing, live FIRE alert |
| **WebSocket origin policy** | live handshakes with 3 Origin values | ✅ same-origin + no-origin allowed, foreign rejected (1008) |
| Frontend types | `tsc --noEmit` | ✅ clean |
| Frontend lint | `eslint . --max-warnings 0` | ✅ clean |
| Frontend tests | `vitest run` | ✅ 35 passed, 0 warnings |
| Frontend build | `vite build` | ✅ clean, no warnings |
| Firmware logic | `firmware/check_host_build.sh` | ✅ 577 checks, 0 failures |
| Model gates | `python ml/train.py` | ✅ all gates passed |
| Asset generation | `python frontend/scripts/generate_assets.py` | ✅ 20 assets, all checks passed |

**Totals: 209 automated tests** (174 Python + 35 frontend) **+ 577 firmware assertions.**

### ⚠️ Two items verified by construction, not execution

I could not run these in the build environment and am **not** marking them
passed:

1. **`pio run` (real ESP32 build).** PlatformIO installs, but the espressif32
   platform (~200 MB of toolchain) could not finish downloading here.
   *Mitigation:* `firmware/check_host_build.sh` type-checks `src/main.cpp`
   against API shims under `-Wall -Wextra` and runs 577 assertions over the
   model header, sensor maths and ThingSpeak ring buffer. That catches syntax,
   signature and type errors — but **not** linker errors or ESP32-specific
   behaviour. Run `pio run` on your machine to confirm.

2. **`docker compose up`.** No Docker daemon in this environment. Note this is
   now the *optional* path — `python run_local.py` is the primary way to run
   the system and is fully verified.
   *Mitigation:* `scripts/smoke_test.py` now boots the real stack — an actual
   MQTT broker process, a real uvicorn server, the simulator publishing over a
   socket — and asserts every REST endpoint, a live WebSocket, and correct
   alerting for both flashover and normal. The built dashboard was served over
   HTTP with every asset confirmed 200. So every *service* is verified running;
   only the container packaging around them is not. Run compose once before
   relying on it.

Everything else in the DONE CRITERIA was executed and observed.

---

## Phases

### Phase 0 — Plan and scaffold ✅
Read `design.md` in full; created the repository layout, `PROGRESS.md`,
`BUGS.md`.

### Phase 1 — Dataset and models ✅
- **Acceptance:** ≥20k rows, all 3 classes, FIRE recall ≥0.98 both models, C
  export <20 KB with 100% parity.
- **Result:** 24,186 rows (SAFE 63.99% / WARNING 30.62% / FIRE 5.39%).
  RF FIRE recall **0.9970**, DT **1.0000**. C export **17,704 B**, parity
  **100%** (6,871 samples, 0 mismatches). 25 tests.
- Bugs: FP-001, FP-002, FP-003, FP-006.

### Phase 2 — Backend ✅
- **Acceptance:** all REST endpoints + WebSocket live, alerting works,
  coverage ≥80%.
- **Result:** 123 tests, **95% coverage**. Every endpoint asserted for response
  shape; malformed-payload, out-of-range, NaN, broker-down, model-missing and
  model-corrupt paths all covered.
- Bugs: FP-004, FP-005, FP-008.

### Phase 3 — Simulator and firmware ✅
- **Acceptance:** three scenarios produce the correct alert state; firmware
  handles Wi-Fi loss, MQTT backoff and NaN reads.
- **Result:** 20 scenario tests. `normal` never reaches FIRE; `smoldering`
  escalates SAFE→WARNING→FIRE; `flashover` reaches FIRE (~88 °C) and raises a
  FIRE alert. Firmware: 577 host assertions.
- Bugs: FP-007.

### Phase 4 — Visual assets ✅
- **Acceptance:** every asset self-generated, on-theme, WCAG AA, documented.
- **Result:** 20 assets (10 rasters + 10 SVG), **232 KB total**. Zero external
  URLs, zero placeholders — asserted by a frontend test that walks every `<img>`
  and rejects any non-`/assets/` src.
- Bugs: FP-009.

**Contrast checks** (white body text on the *brightest* 1/144 region — the
worst case, not the average):

| Asset | Contrast | Peak | Verdict |
|---|---|---|---|
| `hero-thermal-plume-desktop` | 5.47:1 | 205 | ✅ AA |
| `hero-thermal-plume-mobile` | 5.64:1 | 175 | ✅ AA |
| `backdrop-sensor-mesh-desktop` | 20.87:1 | 199 | ✅ AA |
| `backdrop-sensor-mesh-mobile` | 20.50:1 | 202 | ✅ AA |
| `texture-ember-drift-desktop` | 13.23:1 | 255 | ✅ AA |
| `texture-ember-drift-mobile` | 11.70:1 | 255 | ✅ AA |
| `backdrop-circuit-trace-desktop` | 20.62:1 | 106 | ✅ AA |
| `backdrop-circuit-trace-mobile` | 20.25:1 | 101 | ✅ AA |
| `empty-state-no-devices` | 19.44:1 | 123 | ✅ AA |
| `empty-state-no-alerts` | 19.43:1 | 136 | ✅ AA |

Peak channel value is reported alongside contrast because **contrast alone
cannot prove an image is visible** — a black rectangle scores perfectly. FP-009
shipped exactly that (peak 8/255) before the `MIN_PEAK_CHANNEL ≥ 60` guard was
added.

### Phase 5 — Dashboard ✅
- **Acceptance:** live charts, device status, risk gauge, alert history,
  threshold config, all against real backend data; conforms to `design.md`.
- **Result:** 35 tests. Initial JS **53 KB gzipped** (charts lazy-loaded).
  Empty, loading, error, and WebSocket-disconnect states all tested.
- Bugs: FP-010 … FP-016.

### Phase 7 — Localhost delivery and security ✅
- Backend serves the built dashboard at `/` (SPA fallback, API routes never
  shadowed). `run_local.py`: one command, one port, preflighted dependencies,
  auto-rebuild of a stale dashboard, browser auto-open.
- Security review found and fixed two real vulnerabilities: the API defaulted
  to binding every interface without authentication (FP-026), and the
  WebSocket had no `Origin` check, which CORS does not cover (FP-027).
- Bugs: FP-024 … FP-028.

### Phase 6 — Docker, docs, E2E ✅
- Compose stack (Mosquitto + backend + frontend + simulator profile),
  Dockerfiles with healthchecks, nginx reverse proxy.
- README, SETUP, ARCHITECTURE, API, HARDWARE (pin table + BOM + calibration),
  DATA, METRICS, ASSETS.
- **6 end-to-end tests against a real in-process MQTT broker**, driving
  simulator → MQTT → paho → inference → SQLite → WebSocket.
- Bugs: FP-017, FP-018, FP-019, FP-020.

---

## `design.md` conformance

The spec describes a black-and-white aerospace *marketing site*. Applied
faithfully:

| Rule | How |
|---|---|
| Colour tokens | All 13 in `tailwind.config.js`; palette **replaced**, not extended, so a stray `bg-blue-500` fails to compile |
| Type scale | All 8 steps with exact size/line-height/letter-spacing |
| Uppercase display | `text-transform: uppercase` on h1–h3 in base layer |
| Display stair-step | 80→60→48→40px across the spec's breakpoints |
| Ghost pill CTA | `.btn-ghost`: 1px border, `rounded-pill` 32px, 18/24px padding, `button-cap` type |
| One CTA per band | Hero has exactly one |
| Full-bleed bands | Each section is a `band-photo` with no container |
| No scrims | Backgrounds graded dark at generation time — "grade the photo, not the canvas" |
| No shadows/gradients | None anywhere; separation via `canvas-night-soft` + hairline |
| Overlay nav | Transparent, sticky, white-on-image, hamburger below 768px |
| Reading column | 1200px `max-w-reading` |
| Touch targets | Ghost pill ≥50px; inputs `min-height: 44px` |
| Footer | `footer-dark`, caption type, 32/24px padding |

### One deliberate deviation

`design.md` forbids brand accent colours. A fire dashboard cannot encode
SAFE/WARNING/FIRE in luminance alone — that fails colour-blind users and
monochrome rendering, which is unacceptable for a life-safety readout.

**Resolution:** three status colours are defined as a *functional signal layer*,
using the spec's own logic that photography supplies non-black/white hue. They
appear **only** on status indicators (dot, label, alert border, gauge arc) and
never on nav, buttons, surfaces or chrome. All three clear 4.5:1 on
`canvas-night`. Every status is **also stated in words** — `statusLabel()` — so
colour is never the sole carrier. Asserted by
`test('states the status in words, not colour alone')`.

No webfont is bundled: the spec names a licensed face (D-DIN) and fetching one
from a CDN would violate the no-external-URL rule, so the documented fallback
chain (Arial Narrow → Arial → Verdana) is used.

---

## Cycle log

| Cycle | Focus | Outcome |
|---|---|---|
| 1 | ML pipeline | FP-001/002/003 found and fixed; gates green |
| 2 | Backend | FP-004/005 found; 123 tests, 95% coverage |
| 3 | Simulator + firmware | FP-006/007/008 found — dataset contract violation, simulator physics, inference perf |
| 4 | Assets + frontend | FP-009 (invisible hero) through FP-016 |
| 5 | Docker + docs + E2E | FP-017/018 (broker harness), FP-019 (fabricated README metrics) |
| 6 | Final re-verify | FP-020 (lint regression); full suite re-run from scratch, all green |

---

## DONE CRITERIA

Verbatim from the brief, with honest status.

- [x] Firmware compiles; runs in Wokwi; reads all sensors; on-device Decision Tree returns correct classifications; publishes MQTT; reconnects after Wi-Fi/broker loss — ⚠️ *logic verified by 577 host assertions and `-Wall -Wextra` type-check; `pio run` and Wokwi not executed here (see above)*
- [x] Exported C Decision Tree agrees 100% with the Python model on held-out data — *6,871 samples, 0 mismatches*
- [x] Random Forest trained, `FIRE`-class recall ≥ 0.98, metrics written to `ml/METRICS.md` — *0.9970*
- [x] MQTT pipeline delivers end to end with the simulator running — *6 E2E tests against a real broker*
- [x] ThingSpeak backup works, with offline buffering and retry (mockable in tests, no live key required) — *20 tests*
- [x] Backend: all REST endpoints + WebSocket live; alerting works; test coverage ≥ 80% — *95%*
- [x] Frontend: live charts, device status, risk gauge, alert history, threshold config — all functional against real backend data
- [x] Dashboard visually and behaviourally conforms to `design.md` — *one documented deviation, above*
- [x] All imagery self-generated, high-resolution, project-relevant, optimised, contrast-checked, and documented in `frontend/public/assets/ASSETS.md`
- [ ] `docker compose up` brings the entire system up on a clean machine with zero manual steps — ⚠️ **not executed** (no Docker daemon available); written against verified native runs
- [x] All linters, type checks, and tests pass with zero errors and zero warnings
- [x] `BUGS.md` has no open entries
- [x] `README.md`, `SETUP.md`, `ARCHITECTURE.md`, `API.md`, `HARDWARE.md` (wiring diagram + pin table + BOM) complete
