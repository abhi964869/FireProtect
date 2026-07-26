/* Host compile-check shim for ESP32 HTTPClient - see host_shims/README.md. */
#ifndef FIREPROTECT_HOST_SHIM_HTTPCLIENT_H
#define FIREPROTECT_HOST_SHIM_HTTPCLIENT_H

#include <Arduino.h>

class HTTPClient {
  public:
    bool begin(const char *) { return true; }
    void setTimeout(uint16_t) {}
    int GET() { return 200; }
    void end() {}
};

#endif /* FIREPROTECT_HOST_SHIM_HTTPCLIENT_H */
