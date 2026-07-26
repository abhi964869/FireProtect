/* Host compile-check shim for WiFi.h - see host_shims/README.md. */
#ifndef FIREPROTECT_HOST_SHIM_WIFI_H
#define FIREPROTECT_HOST_SHIM_WIFI_H

#include <Arduino.h>

#define WL_CONNECTED 3
#define WIFI_STA 1

class WiFiClass {
  public:
    int status() const { return WL_CONNECTED; }
    void mode(int) {}
    void begin(const char *, const char *) {}
    void disconnect(bool = false) {}
    IPAddress localIP() const { return IPAddress(); }
};
inline WiFiClass WiFi;

class WiFiClient {};

#endif /* FIREPROTECT_HOST_SHIM_WIFI_H */
