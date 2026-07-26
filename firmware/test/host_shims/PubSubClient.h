/* Host compile-check shim for PubSubClient - see host_shims/README.md. */
#ifndef FIREPROTECT_HOST_SHIM_PUBSUBCLIENT_H
#define FIREPROTECT_HOST_SHIM_PUBSUBCLIENT_H

#include <Arduino.h>
#include <WiFi.h>

class PubSubClient {
  public:
    explicit PubSubClient(WiFiClient &) {}
    void setServer(const char *, uint16_t) {}
    void setBufferSize(uint16_t) {}
    void setKeepAlive(uint16_t) {}
    bool connected() const { return true; }
    int state() const { return 0; }
    bool loop() { return true; }
    bool publish(const char *, const char *, bool = false) { return true; }
    bool connect(const char *) { return true; }
    bool connect(const char *, const char *, uint8_t, bool, const char *) {
        return true;
    }
    bool connect(const char *, const char *, const char *, const char *, uint8_t,
                 bool, const char *) {
        return true;
    }
};

#endif /* FIREPROTECT_HOST_SHIM_PUBSUBCLIENT_H */
