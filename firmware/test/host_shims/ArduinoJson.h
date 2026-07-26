/* Host compile-check shim for ArduinoJson v7 - see host_shims/README.md.
 *
 * Models just enough of the API surface main.cpp uses: JsonDocument with
 * operator[] assignment of mixed types, and serializeJson into a char buffer.
 */
#ifndef FIREPROTECT_HOST_SHIM_ARDUINOJSON_H
#define FIREPROTECT_HOST_SHIM_ARDUINOJSON_H

#include <Arduino.h>
#include <cstddef>

class JsonVariantShim {
  public:
    JsonVariantShim &operator=(float) { return *this; }
    JsonVariantShim &operator=(double) { return *this; }
    JsonVariantShim &operator=(int) { return *this; }
    JsonVariantShim &operator=(const char *) { return *this; }
    JsonVariantShim &operator=(bool) { return *this; }
};

class JsonDocument {
  public:
    JsonVariantShim operator[](const char *) { return JsonVariantShim(); }
};

inline size_t serializeJson(const JsonDocument &, char *out, size_t capacity) {
    if (capacity == 0) {
        return 0;
    }
    const char *sample = "{}";
    std::strncpy(out, sample, capacity - 1);
    out[capacity - 1] = '\0';
    return std::strlen(out);
}

#endif /* FIREPROTECT_HOST_SHIM_ARDUINOJSON_H */
