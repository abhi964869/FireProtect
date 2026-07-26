/*
 * thingspeak_buffer.h - ThingSpeak mirror with offline buffering and retry.
 *
 * The ESP32 has no reliable filesystem guarantee across power loss in this
 * design, so the buffer is a fixed-size RAM ring. When the network is down,
 * entries accumulate; when it returns, they drain oldest-first. The ring
 * overwrites the oldest entry when full, on the same reasoning as the backend:
 * during an incident, recent data matters more than stale data.
 */

#ifndef FIREPROTECT_THINGSPEAK_BUFFER_H
#define FIREPROTECT_THINGSPEAK_BUFFER_H

#include <stdint.h>
#include <string.h>

#ifdef ARDUINO
#include <Arduino.h>
#include <HTTPClient.h>
#endif

/** One buffered mirror entry. Kept small - this lives in RAM. */
struct ThingSpeakEntry {
    float temperatureC = 0.0f;
    float humidityPct = 0.0f;
    float smokePpm = 0.0f;
    float airQualityPpm = 0.0f;
    float flameVolts = 0.0f;
    int flameDetected = 0;
    int status = 0;
};

/* 64 entries x ~28 bytes = ~1.8 KB. At the 20 s mirror interval that is
 * ~21 minutes of outage before the oldest samples start being overwritten. */
#define THINGSPEAK_BUFFER_CAPACITY 64

/* A 4xx other than 429 will never succeed on retry, so it is dropped. */
#define THINGSPEAK_MAX_RETRIES 3

class ThingSpeakBuffer {
  public:
    void begin(const char *apiKey) {
        m_apiKey = apiKey;
        m_head = 0;
        m_count = 0;
        m_dropped = 0;
        m_sent = 0;
    }

    size_t pending() const { return m_count; }
    uint32_t dropped() const { return m_dropped; }
    uint32_t sent() const { return m_sent; }

    /** Queue an entry, then drain one if the network is up. */
    void submit(const ThingSpeakEntry &entry, bool online) {
        push(entry);
        if (online) {
            drainOne();
        }
    }

    /** Append, overwriting the oldest entry when the ring is full. */
    void push(const ThingSpeakEntry &entry) {
        const size_t tail = (m_head + m_count) % THINGSPEAK_BUFFER_CAPACITY;
        m_entries[tail] = entry;
        if (m_count == THINGSPEAK_BUFFER_CAPACITY) {
            m_head = (m_head + 1) % THINGSPEAK_BUFFER_CAPACITY;
            m_dropped++;
        } else {
            m_count++;
        }
    }

    /** Remove and return the oldest entry. Returns false when empty. */
    bool pop(ThingSpeakEntry &out) {
        if (m_count == 0) {
            return false;
        }
        out = m_entries[m_head];
        m_head = (m_head + 1) % THINGSPEAK_BUFFER_CAPACITY;
        m_count--;
        return true;
    }

    /** Peek at the oldest entry without removing it. */
    bool peek(ThingSpeakEntry &out) const {
        if (m_count == 0) {
            return false;
        }
        out = m_entries[m_head];
        return true;
    }

#ifdef ARDUINO
    /**
     * Try to upload the oldest buffered entry.
     *
     * Only pops on success, so a failed send leaves the entry queued for the
     * next attempt. One entry per call keeps the main loop responsive.
     */
    bool drainOne() {
        ThingSpeakEntry entry;
        if (!peek(entry)) {
            return false;
        }
        if (m_apiKey == nullptr || strlen(m_apiKey) == 0) {
            return false;
        }

        char url[320];
        snprintf(url, sizeof(url),
                 "http://api.thingspeak.com/update?api_key=%s"
                 "&field1=%.2f&field2=%.2f&field3=%.2f&field4=%.2f"
                 "&field5=%.3f&field6=%d&field7=%d&field8=%d",
                 m_apiKey, entry.temperatureC, entry.humidityPct, entry.smokePpm,
                 entry.airQualityPpm, entry.flameVolts, entry.flameDetected,
                 entry.status, entry.status);

        HTTPClient http;
        http.setTimeout(8000);
        if (!http.begin(url)) {
            return false;
        }
        const int code = http.GET();
        http.end();

        if (code >= 200 && code < 300) {
            pop(entry);
            m_sent++;
            return true;
        }
        if (code >= 400 && code < 500 && code != 429) {
            Serial.printf("[thingspeak] permanent error %d; dropping entry\n", code);
            pop(entry);
            m_dropped++;
            return false;
        }
        Serial.printf("[thingspeak] transient error %d; will retry\n", code);
        return false;
    }
#else
    bool drainOne() { return false; }
#endif

  private:
    ThingSpeakEntry m_entries[THINGSPEAK_BUFFER_CAPACITY];
    size_t m_head = 0;
    size_t m_count = 0;
    uint32_t m_dropped = 0;
    uint32_t m_sent = 0;
    const char *m_apiKey = nullptr;
};

#endif /* FIREPROTECT_THINGSPEAK_BUFFER_H */
