# FireProtect — Bug Log

Every defect found during the build, its root cause, and its fix. Format:

`ID | severity | component | symptom | root cause | fix | verification | status`

**Open entries: 0.**

---

### FP-001 | critical | ml/export_tree.py

- **Symptom:** `ValueError: Invalid format specifier` on the first training run.
- **Root cause:** `f"{threshold:.9ff}"` — the trailing `f` intended as C's float
  literal suffix was parsed as part of the format spec.
- **Fix:** Moved the suffix outside the replacement field: `f"{threshold:.9f}f"`.
- **Verification:** `python ml/train.py`
- **Status:** ✅ Fixed

---

### FP-002 | major | ml/train.py

- **Symptom:** `PermissionError: Operation not permitted` deleting
  `firmware/include/_parity_harness.c`.
- **Root cause:** The C parity harness was written into `firmware/include/`, a
  source directory, and cleaned up in place. Build artefacts do not belong in
  a source tree, and the cleanup assumed unlink always succeeds.
- **Fix:** Harness and binary now build in `tempfile.mkdtemp()`, removed via
  `shutil.rmtree(..., ignore_errors=True)` in a `finally`. The header is
  included by absolute path.
- **Verification:** `python ml/train.py`
- **Status:** ✅ Fixed

---

### FP-003 | critical | firmware/include/fire_model.h

- **Symptom:** Exported C disagreed with the Python model on 2 of 6,871
  held-out samples (99.9709% agreement, target 100%).
- **Root cause:** The generated C used `float` (32-bit) while scikit-learn
  evaluates `X[f] <= threshold` in float64. Samples within one ULP of a split
  threshold fell on the opposite side after narrowing.
- **Fix:** Generated code now uses a `fire_feature_t` typedef of `double`,
  thresholds are emitted at 17 significant digits, and the parity harness feeds
  values with `%.17g` / reads with `%lf`. Cost on-device is a few microseconds
  per inference for a depth-9 tree.
- **Verification:** `python ml/train.py` → `parity 100.0000%`;
  `pytest ml/tests/test_ml.py::test_export_parity_on_freshly_trained_tree`
- **Status:** ✅ Fixed

---

### FP-004 | major | backend (all modules)

- **Symptom:** `ImportError: cannot import name 'UTC' from 'datetime'`.
- **Root cause:** `datetime.UTC` was added in Python 3.11; the project targets
  3.10+.
- **Fix:** Switched to `timezone.utc` throughout and added `UP017` to ruff's
  ignore list so pyupgrade does not reintroduce it.
- **Verification:** `pytest`, `ruff check backend ml simulator`
- **Status:** ✅ Fixed

---

### FP-005 | major | backend/tests

- **Symptom:** `pytest backend/tests/test_api.py` never completed.
- **Root cause:** Each test built a `TestClient`, whose lifespan called
  `MqttSubscriber.start()`. Every test therefore dialled a broker and paid
  connect/retry cost, making the suite depend on external infrastructure.
- **Fix:** The `client` fixture stubs `start`/`stop`. The MQTT layer is covered
  directly in `test_mqtt.py` (via `handle_message`) and genuinely end-to-end in
  `test_e2e_mqtt.py` against a real in-process broker, so no coverage is lost.
- **Verification:** `pytest backend/tests` — 43 API tests in ~11 s
- **Status:** ✅ Fixed

---

### FP-006 | critical | ml/generate_dataset.py

- **Symptom:** Dataset contained `smoke_ppm` values up to 262,701 — above the
  100,000 ceiling `TelemetryIn` enforces. The generator was producing data the
  system's own ingest contract would reject.
- **Root cause:** The MQ ppm curve is a power law with exponent ≈ −2.2 and is
  asymptotic near zero Rs/R₀. No saturation ceiling was modelled, but real MQ
  sensors pin at the top of their detection range.
- **Fix:** Added `MQ2_MAX_PPM` / `MQ135_MAX_PPM` (10,000 ppm, datasheet range)
  and clamped in `_apply_transducers`. Mirrored in `firmware/include/sensors.h`
  and the simulator so all three agree. Documented in `ml/DATA.md`. Dataset
  regenerated and models retrained.
- **Verification:** dataset max `smoke_ppm` = 10,000; `pytest ml/tests`
- **Status:** ✅ Fixed

---

### FP-007 | critical | simulator/virtual_device.py

- **Symptom:** The `flashover` scenario stalled at ~45 °C after 3 minutes and
  never reached FIRE. `test_flashover_scenario_reaches_fire` failed.
- **Root cause:** `_drift()` applied ambient mean-reversion
  (`temp += 0.05 * (22 - temp)`) to the *total* temperature. At 47 °C that
  removed 1.25 °C per tick while the fire added 1.4 °C — the reversion was
  cancelling the fire's own heat release. Physically wrong: a burning room does
  not revert to ambient.
- **Fix:** Rewrote `SensorState` to track baseline and event contributions
  separately (`baseline_temp_c` + `excess_temp_c`, multiplicative
  `mq2_depression` over a drifting baseline). Only the baseline reverts; event
  excess decays slowly on its own. Flashover now reaches ~88 °C.
- **Verification:** `pytest simulator/tests` — all 20 scenario tests pass
- **Status:** ✅ Fixed

---

### FP-008 | major | backend/app/inference.py

- **Symptom:** Ingest ran at ~87 ms per reading; scenario tests exceeded their
  time budget.
- **Root cause:** The forest is fitted with `n_jobs=-1` and joblib persists it.
  At serving time we classify one row at a time, where joblib's worker-pool
  setup dominates: ~85 ms of overhead for ~1 ms of work.
- **Fix:** `FireClassifier.load` sets `model.n_jobs = 1` after loading.
  ~50x faster on this workload, and it frees CPU for the rest of the request.
- **Verification:** 90 predictions dropped from 7.81 s to ~0.15 s
- **Status:** ✅ Fixed

---

### FP-009 | critical | frontend/scripts/generate_assets.py

- **Symptom:** `hero-thermal-plume-desktop.webp` was 7 KB at 2560×1440 with a
  peak channel value of **8/255** — an effectively black rectangle. It passed
  the contrast check at 20.77:1 because a black image trivially passes.
- **Root cause:** Two compounding errors. (1) Plume layers were combined with
  `Image.blend(a, b, 0.55)`, which *averages* — nine layers diluted each other
  instead of accumulating light. (2) `_radial_falloff` used a pure quadratic
  ramp, putting full intensity only at the centre pixel; the vignette then
  multiplied what little remained.
- **Fix:** Light now accumulates via `ImageChops.lighter`; `_radial_falloff`
  gained a `plateau` parameter so a source holds full intensity across an inner
  radius; vignettes widened to darken only the rim. **Added `MIN_PEAK_CHANNEL`
  guard** — export now fails if peak channel < 60, because contrast alone can
  never prove an image is visible.
- **Verification:** `python frontend/scripts/generate_assets.py` — hero peak now
  205/255, contrast 5.47:1
- **Status:** ✅ Fixed

---

### FP-010 | major | frontend/tsconfig.node.json

- **Symptom:** `tsc --noEmit` failed with TS6305 and TS6310.
- **Root cause:** `tsconfig.node.json` is a referenced composite project;
  `composite: true` requires emit, so `noEmit: true` is contradictory. The main
  config also included `vite.config.ts` directly, double-owning the file.
- **Fix:** Removed `noEmit` from the node config, gave it an `outDir`, and
  restricted the main config's `include` to `src`.
- **Verification:** `npx tsc --noEmit`
- **Status:** ✅ Fixed

---

### FP-011 | minor | frontend/src/lib/api.ts

- **Symptom:** `TS2339: Property 'env' does not exist on type 'ImportMeta'`.
- **Root cause:** Vite's client types were never referenced.
- **Fix:** Added `src/vite-env.d.ts` with `/// <reference types="vite/client" />`
  and a typed `ImportMetaEnv` documenting `VITE_API_BASE`. Added `@types/node`
  for `vite.config.ts`'s `node:path` import.
- **Verification:** `npx tsc --noEmit`
- **Status:** ✅ Fixed

---

### FP-012 | minor | frontend/src/test/App.test.tsx

- **Symptom:** Two tests failed — "surfaces a backend outage" (element not
  found) and "ignores a malformed frame" (saw `Connecting`, expected `Live`).
- **Root cause:** Both were test defects, not app defects. (1) With the backend
  down, both the page banner and the threshold panel render `role="alert"`, so
  the unscoped role query was ambiguous. (2) The second asserted synchronously
  against a React state update that had not flushed.
- **Fix:** Added `data-testid="load-error"` and scoped the query to it (still
  asserting the `role`); added a `waitFor` before asserting the state persists.
  Neither assertion was weakened.
- **Verification:** `npx vitest run` — 35/35
- **Status:** ✅ Fixed

---

### FP-013 | minor | frontend/src/test/setup.ts

- **Symptom:** Recharts logged `width(0) and height(0) of chart should be
  greater than 0` on every chart render, burying real output.
- **Root cause:** jsdom performs no layout, so `ResponsiveContainer` measures
  0×0. Driving it through a stubbed `ResizeObserver` only traded the warning
  for an `act()` warning, since the resulting setState landed outside act.
- **Fix:** Mocked `ResponsiveContainer` to clone its child chart with explicit
  `width`/`height`. Charts render with real geometry and the output is clean.
- **Verification:** `npx vitest run` — no warnings
- **Status:** ✅ Fixed

---

### FP-014 | minor | frontend/src/test/App.test.tsx

- **Symptom:** `An update to App inside a test was not wrapped in act(...)`.
- **Root cause:** Two sources — the mock WebSocket's `open`/`emit`/`close` call
  React setState directly, and `ThresholdConfig` fetches independently of
  `useLiveData`, settling after some tests had finished asserting.
- **Fix:** Wrapped every socket interaction in `act()`, and added a
  `renderApp()` helper that waits for the threshold panel to reach a terminal
  state before a test proceeds.
- **Verification:** `npx vitest run` — zero warnings
- **Status:** ✅ Fixed

---

### FP-015 | minor | frontend/src/test/setup.ts

- **Symptom:** ESLint error — `` `import()` type annotations are forbidden ``
  (`@typescript-eslint/consistent-type-imports`).
- **Root cause:** `importOriginal<typeof import('recharts')>()` uses an inline
  `import()` type, which the project's own rule forbids.
- **Fix:** Added a top-level `import type * as Recharts from 'recharts'` and a
  `Rechartsheet` alias. Rule kept enabled.
- **Verification:** `npx eslint . --max-warnings 0`
- **Status:** ✅ Fixed

---

### FP-016 | minor | frontend/vite.config.ts

- **Symptom:** Build warned `Some chunks are larger than 500 kB` (recharts at
  525 kB).
- **Root cause:** Recharts is a single vendor library that cannot be split
  further, and it was in the initial bundle.
- **Fix:** Made `SensorCharts` a `lazy()` import behind `Suspense`, so recharts
  loads on demand and never blocks first paint (initial JS: 167 kB → 53 kB
  gzipped). Raised `chunkSizeWarningLimit` to 600 with a comment explaining
  that the remaining chunk is async and unsplittable, so the default threshold
  would cry wolf on every build.
- **Verification:** `npx vite build` — clean, no warnings
- **Status:** ✅ Fixed

---

### FP-017 | major | backend/tests/test_e2e_mqtt.py

- **Symptom:** `RuntimeError: no running event loop` starting the test broker.
- **Root cause:** `amqtt.broker.Broker.__init__` calls
  `asyncio.get_running_loop()`, so it must be constructed inside the loop, not
  before `run_until_complete`.
- **Fix:** Moved construction into the async `boot()` coroutine.
- **Verification:** `pytest backend/tests/test_e2e_mqtt.py`
- **Status:** ✅ Fixed

---

### FP-018 | major | backend/tests/test_e2e_mqtt.py

- **Symptom:** Broker started but every client was dropped:
  `Failed to initialize client session: No more data`.
- **Root cause:** The broker was configured with `"plugins": {}`. With no auth
  plugin registered, amqtt denies every CONNECT during session setup — which
  surfaces as a protocol-looking error rather than an auth failure.
- **Fix:** Explicitly configured `AnonymousAuthPlugin` with
  `allow_anonymous: true` (also removing the deprecated `auth`/`topic-check`
  keys and their warnings).
- **Verification:** `pytest backend/tests/test_e2e_mqtt.py` — 6/6
- **Status:** ✅ Fixed

---

### FP-019 | major | README.md

- **Symptom:** The model performance table quoted accuracy 0.9915 / 0.9808 and
  macro F1 0.9834 / 0.9645 — figures that appear nowhere in `ml/metrics.json`.
  They were written from expectation rather than read from the artefact.
- **Root cause:** Documentation authored before cross-checking against the
  generated metrics.
- **Fix:** Replaced with the actual values (RF accuracy 0.9121 / macro F1
  0.9185; DT 0.8399 / 0.8654) and added an explanation of *why* overall
  accuracy is deliberately not the headline — the class weighting trades it for
  FIRE recall on purpose.
- **Verification:** Every figure in `README.md` now matches `ml/metrics.json`
- **Status:** ✅ Fixed

---

### FP-021 | major | simulator/virtual_device.py

- **Symptom:** Running the live-stack smoke test with `--interval 0.25` to
  speed up the demo, the `flashover` scenario never reached FIRE — it sat at
  SAFE for the whole run.
- **Root cause:** `--interval` controlled *both* the wall-clock publish rate
  and the simulated time advanced per tick (`device.tick(args.interval)`).
  Every scenario ramp is driven by `state.elapsed_s`, so publishing 8x faster
  also made the fire develop 8x slower. Nobody would guess that from the flag
  name, and it makes the simulator useless for fast demos.
- **Fix:** Added `--sim-step`, the simulated seconds advanced per tick,
  defaulting to `--interval` so existing behaviour is unchanged. Now
  `--interval 0.25 --sim-step 2` replays a fire at 8x speed with its shape
  intact. Documented in the flag help and in SETUP.md.
- **Verification:** `python scripts/smoke_test.py --scenario flashover` — FIRE
  alert raised in ~12 s of wall clock instead of ~3 min
- **Status:** ✅ Fixed

---

### FP-022 | minor | scripts/smoke_test.py

- **Symptom:** The flashover check intermittently failed with "none raised" —
  the run ended at WARNING before the fire had ramped to FIRE.
- **Root cause:** The wait loop used a flat wall-clock budget (`--seconds`).
  Time-to-FIRE varies with machine speed and MQTT scheduling, so a fixed budget
  is a race: too short and the scenario is cut off mid-ramp, which looks like a
  detection failure rather than an impatient test.
- **Fix:** The loop now waits for the scenario's *expected outcome* and exits
  as soon as it lands, with `--seconds` demoted to a generous ceiling (60 s vs
  a typical ~12 s). If the ceiling is hit it says so explicitly instead of
  silently reporting a failed check.
- **Verification:** `python scripts/smoke_test.py --scenario flashover` — passes
- **Status:** ✅ Fixed

---

### FP-023 | minor | scripts/smoke_test.py

- **Symptom:** `--keep-running` left a working stack on a randomly chosen port,
  but the dashboard could not talk to it — the Vite dev proxy targets
  `localhost:8000`.
- **Root cause:** The script always called `free_port()`, which is right for an
  automated check but wrong for the interactive "leave it up and browse it"
  mode it also advertises.
- **Fix:** Added `--api-port` / `--mqtt-port`. `--keep-running` now prints the
  live API URLs, and either the exact command to start the dashboard against it
  (when pinned to 8000) or a warning that the UI will not connect on a random
  port.
- **Verification:** `python scripts/smoke_test.py --api-port 8000` — passes
- **Status:** ✅ Fixed

---

### FP-024 | critical | frontend + backend (deployment topology)

- **Symptom:** "unable to see it at localhost". Getting the dashboard up needed
  two terminals, an `npm install`, and a port that happened to match the Vite
  dev proxy. Every one of those was a chance to end up with nothing in the
  browser.
- **Root cause:** The dashboard was only ever served by a *separate* process —
  the Vite dev server or an nginx container. There was no path where the thing
  you start is the thing you open. The proxy hard-codes `localhost:8000`, so a
  backend on any other port silently produced a blank page.
- **Fix:** The backend now serves the built dashboard itself at `/`, mounted
  after the API routes so it can never shadow them, with an SPA fallback for
  deep links (and genuine 404s preserved for missing assets). Added
  `run_local.py`: one command that preflights every dependency, starts broker +
  backend + simulator, and opens the browser. One process tree, one port, no
  proxy, no CORS.
- **Verification:** `python run_local.py` — `/` returns the dashboard, all
  assets 200, deep link `/devices` 200, missing asset 404, live FIRE alert
- **Status:** ✅ Fixed

---

### FP-025 | major | run_local.py / backend/app/db.py

- **Symptom:** The backend "did not start" with no further explanation, after a
  60-second stall.
- **Root cause:** Two compounding problems. (1) SQLite cannot operate on
  filesystems that do not implement real file locking — network shares,
  cloud-synced folders (OneDrive/Dropbox), some virtualised mounts — and fails
  with `disk I/O error` or hangs. The project database sat in the repo, so
  anyone with the project on such a drive was dead in the water. (2) The
  launcher sent backend output to `DEVNULL`, so the actual error was discarded.
- **Fix:** `run_local.py` probes whether SQLite can genuinely create and commit
  to the intended location before starting, and falls back to the OS temp
  directory with an explicit warning if not. Backend output now goes to a log
  file, and startup failure prints the last 25 lines plus the log path.
- **Verification:** reproduced on a mount that rejects SQLite; startup now
  succeeds with a clear warning instead of stalling
- **Status:** ✅ Fixed

---

### FP-026 | **critical (security)** | backend/app/config.py

- **Symptom:** `api_host` defaulted to `0.0.0.0`.
- **Root cause:** The API binds every network interface by default and has **no
  authentication**. On any shared network — office, campus, café — anyone could
  read the household's live sensor telemetry, inject fake readings, raise or
  silence alerts, change the fire-alert thresholds via `PUT /api/thresholds`,
  and wipe history via `DELETE /api/readings`. For a fire-detection system,
  remotely raising the thresholds is a safety issue, not just a data issue.
- **Fix:** Default changed to `127.0.0.1`. LAN exposure is now a deliberate
  opt-in (`API_HOST=0.0.0.0`) documented with the auth caveat. `docker-compose`
  sets it explicitly, because inside a container loopback would make the
  published port unreachable — and there only port 8000 is published.
- **Verification:** `test_api_binds_loopback_by_default`
- **Status:** ✅ Fixed

---

### FP-027 | **major (security)** | backend/app/main.py

- **Symptom:** The WebSocket endpoint accepted any handshake regardless of
  `Origin`.
- **Root cause:** Browsers do **not** apply the same-origin policy or CORS
  preflight to WebSocket handshakes. The CORS middleware protecting the REST
  API therefore did nothing for `/ws`. Any web page the user visited while
  FireProtect was running could silently open a socket to it and stream their
  live home sensor data — classic cross-site WebSocket hijacking.
- **Fix:** Added an `Origin` check on the handshake, accepting the configured
  CORS origins plus true same-origin (the dashboard is served by this app), and
  closing with code 1008 otherwise. Requests with **no** `Origin` are allowed:
  those are non-browser clients (the ESP32, curl, tests), and a browser cannot
  suppress the header — which is precisely what makes the check sound.
  Controlled by `WEBSOCKET_STRICT_ORIGIN`.
- **Verification:** 4 tests covering same-origin, allowed-origin, no-origin and
  foreign-origin; confirmed live — foreign origin rejected, dashboard and ESP32
  paths unaffected
- **Status:** ✅ Fixed

---

### FP-028 | minor | run_local.py

- **Symptom:** A committed `frontend/dist` could silently drift behind `src/`,
  serving a dashboard that did not match the code.
- **Root cause:** Committing build output is a deliberate trade (it lets the
  dashboard run with no Node toolchain) but it has no staleness signal.
- **Fix:** `run_local.py` compares the mtime of every frontend source against
  the built `index.html`, rebuilds automatically when npm is available, and
  says so plainly when it cannot.
- **Verification:** touching a source file triggers a rebuild on next start
- **Status:** ✅ Fixed
