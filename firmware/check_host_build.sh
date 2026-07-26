#!/usr/bin/env bash
# Host compile-check for the firmware.
#
# Type-checks src/main.cpp and runs the pure-logic unit tests using host
# compilers only - no ESP32 toolchain required. See test/host_shims/README.md
# for what this does and does not verify.
#
# The authoritative build is `pio run`; this is the fast CI gate.

set -euo pipefail
cd "$(dirname "$0")"

CXX="${CXX:-g++}"

DEFINES=(
  -D ARDUINO=200
  -D FIREPROTECT_VERSION='"1.0.0"'
  -D PUBLISH_INTERVAL_MS=2000
  -D WIFI_SSID='"test-ssid"'
  -D WIFI_PASSWORD='"test-password"'
  -D MQTT_HOST='"localhost"'
  -D MQTT_PORT=1883
  -D MQTT_USERNAME='""'
  -D MQTT_PASSWORD='""'
  -D DEVICE_ID='"esp32-01"'
  -D DEVICE_LOCATION='"Kitchen"'
  -D THINGSPEAK_API_KEY='""'
)

echo "==> Type-checking src/main.cpp"
"$CXX" -std=gnu++17 -Wall -Wextra -fsyntax-only \
  -I test/host_shims -I include \
  "${DEFINES[@]}" \
  src/main.cpp
echo "    OK"

echo "==> Building and running firmware logic tests"
TMPBIN="$(mktemp -d)/firmware_tests"
"$CXX" -std=c++17 -Wall -Wextra -Werror \
  -I include \
  test/test_native/test_firmware_logic.cpp -o "$TMPBIN"
"$TMPBIN"

echo
echo "Host checks passed. Run 'pio run' for the real ESP32 build."
