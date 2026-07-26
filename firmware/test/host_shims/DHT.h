/* Host compile-check shim for the Adafruit DHT library - see README.md. */
#ifndef FIREPROTECT_HOST_SHIM_DHT_H
#define FIREPROTECT_HOST_SHIM_DHT_H

#include <Arduino.h>

#define DHT22 22
#define DHT11 11

class DHT {
  public:
    DHT(uint8_t, uint8_t) {}
    void begin() {}
    float readTemperature() { return 22.0f; }
    float readHumidity() { return 45.0f; }
};

#endif /* FIREPROTECT_HOST_SHIM_DHT_H */
