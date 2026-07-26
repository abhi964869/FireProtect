# Host compile-check shims

These headers are **not** an emulator and never run on hardware. They stub just
enough of the Arduino / ESP32 API surface that `src/main.cpp` uses so a normal
host compiler can type-check it.

## Why they exist

`pio run` needs the full ESP32 toolchain (~200 MB of platform, framework and
GCC cross-compiler). That is the authoritative build and the one that produces
a flashable binary. But it is heavy for CI and impossible in constrained
environments, which means without these shims `main.cpp` would get **no**
compile verification at all — typos, wrong signatures and type errors would sit
undetected until someone with hardware tried to build.

The shims close that gap. They catch:

- syntax errors and typos
- wrong function signatures and argument types
- undeclared identifiers and missing includes
- `-Wall -Wextra` warnings in our own code

They do **not** catch: linker errors, ESP32-specific behaviour, timing, memory
constraints, or anything about real peripheral behaviour. Only `pio run` and
real hardware (or Wokwi) do that.

## Running the check

```bash
cd firmware
g++ -std=gnu++17 -Wall -Wextra -fsyntax-only \
  -I test/host_shims -I include \
  -D ARDUINO=200 -D FIREPROTECT_VERSION='"1.0.0"' -D PUBLISH_INTERVAL_MS=2000 \
  -D WIFI_SSID='"x"' -D WIFI_PASSWORD='"y"' -D MQTT_HOST='"h"' -D MQTT_PORT=1883 \
  -D MQTT_USERNAME='""' -D MQTT_PASSWORD='""' -D DEVICE_ID='"esp32-01"' \
  -D DEVICE_LOCATION='"Kitchen"' -D THINGSPEAK_API_KEY='""' \
  src/main.cpp
```

Or use the wrapper: `./check_host_build.sh`

## Keeping them honest

If a shim's signature drifts from the real library, the host check will pass
while `pio run` fails — which is worse than no check. When you upgrade a
library in `platformio.ini`, re-run the real build and update the shim if the
API changed.
