/*
 * Minimal Arduino / ESP32 shim for HOST COMPILE-CHECKING ONLY.
 *
 * This is not an emulator and never runs on hardware. It exists so `main.cpp`
 * can be type-checked by a normal host compiler in CI, catching typos, wrong
 * signatures and type errors without downloading the full ESP32 toolchain.
 * The real build uses the actual Arduino core via `pio run`.
 *
 * See firmware/test/host_shims/README.md.
 */

#ifndef FIREPROTECT_HOST_SHIM_ARDUINO_H
#define FIREPROTECT_HOST_SHIM_ARDUINO_H

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define ADC_11db 3

using std::min;
using std::max;

inline uint32_t millis() { return 0; }
inline void delay(uint32_t) {}
inline void delayMicroseconds(uint32_t) {}
inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t, uint8_t) {}
inline int digitalRead(uint8_t) { return LOW; }
inline int analogRead(uint8_t) { return 0; }
inline void analogSetAttenuation(int) {}
inline void analogReadResolution(int) {}
inline void tone(uint8_t, unsigned int) {}
inline void noTone(uint8_t) {}

class HostSerial {
  public:
    void begin(unsigned long) {}
    void println() {}
    void println(const char *) {}
    void print(const char *) {}
    int printf(const char *fmt, ...) {
        va_list args;
        va_start(args, fmt);
        const int n = std::vsnprintf(nullptr, 0, fmt, args);
        va_end(args);
        return n;
    }
};
inline HostSerial Serial;

class IPAddress {
  public:
    std::string toString() const { return "0.0.0.0"; }
};

#endif /* FIREPROTECT_HOST_SHIM_ARDUINO_H */
