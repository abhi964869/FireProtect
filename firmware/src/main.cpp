/*
 * FireProtect - ESP32 firmware
 *
 * Reads MQ-2 (smoke/gas), MQ-135 (air quality), DHT22 (temperature/humidity)
 * and an IR flame sensor; classifies locally with an exported decision tree;
 * publishes telemetry over MQTT and mirrors it to ThingSpeak.
 *
 * Design constraints this file is built around:
 *   - The network is unreliable. Wi-Fi and MQTT reconnect with exponential
 *     backoff, and sensor sampling never blocks on either.
 *   - Sensors fail. DHT22 returns NaN on a failed read; every reading is
 *     validated before it reaches the model or the wire.
 *   - The local decision tree is the safety net. If the backend is
 *     unreachable, the device still alarms on its own.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "fire_model.h"
#include "sensors.h"
#include "thingspeak_buffer.h"

// ---------------------------------------------------------------------------
// Pin map - see docs/HARDWARE.md for the full wiring table
// ---------------------------------------------------------------------------

static const uint8_t PIN_MQ2 = 34;         // ADC1_CH6, input-only
static const uint8_t PIN_MQ135 = 35;       // ADC1_CH7, input-only
static const uint8_t PIN_FLAME_ANALOG = 32; // ADC1_CH4
static const uint8_t PIN_FLAME_DIGITAL = 33;
static const uint8_t PIN_DHT = 4;
static const uint8_t PIN_LED_STATUS = 2;   // on-board LED
static const uint8_t PIN_BUZZER = 26;

// ADC2 is unusable while Wi-Fi is active on the ESP32, so every analogue
// sensor above is deliberately on an ADC1 channel. Moving any of them to a
// GPIO in the 0-27 ADC2 range will silently return garbage once Wi-Fi starts.

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------

static const uint32_t SENSOR_INTERVAL_MS = 2000;
static const uint32_t THINGSPEAK_INTERVAL_MS = 20000; // free tier: >= 15 s
static const uint32_t WIFI_RETRY_BASE_MS = 1000;
static const uint32_t WIFI_RETRY_MAX_MS = 60000;
static const uint32_t MQTT_RETRY_BASE_MS = 1000;
static const uint32_t MQTT_RETRY_MAX_MS = 30000;
static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000;

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------

DHT dht(PIN_DHT, DHT22);
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
SensorSuite sensors(PIN_MQ2, PIN_MQ135, PIN_FLAME_ANALOG, PIN_FLAME_DIGITAL);
ThingSpeakBuffer thingspeak;

static char g_telemetryTopic[96];
static char g_alertTopic[96];
static char g_statusTopic[96];

static uint32_t g_lastSensorRead = 0;
static uint32_t g_lastThingSpeak = 0;
static uint32_t g_nextWifiAttempt = 0;
static uint32_t g_nextMqttAttempt = 0;
static uint32_t g_wifiBackoffMs = WIFI_RETRY_BASE_MS;
static uint32_t g_mqttBackoffMs = MQTT_RETRY_BASE_MS;

// Previous sample, for the rate-of-change features the model expects.
static bool g_havePrevious = false;
static float g_prevTemperature = 0.0f;
static float g_prevSmoke = 0.0f;
static uint32_t g_prevSampleMs = 0;

static int g_lastStatus = FIRE_MODEL_SAFE;
static uint32_t g_consecutiveFire = 0;

//: Consecutive FIRE classifications before the local buzzer sounds. Matches
//: the backend's debounce so device and server agree on what counts as a fire.
static const uint32_t LOCAL_ALARM_CONSECUTIVE = 2;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** NOAA Rothfusz heat index in Celsius. Mirrors the Python implementation. */
static float computeHeatIndex(float tempC, float humidity) {
    const float tF = tempC * 9.0f / 5.0f + 32.0f;
    float hiF;
    if (tF >= 80.0f) {
        hiF = -42.379f + 2.04901523f * tF + 10.14333127f * humidity -
              0.22475541f * tF * humidity - 6.83783e-3f * tF * tF -
              5.481717e-2f * humidity * humidity +
              1.22874e-3f * tF * tF * humidity +
              8.5282e-4f * tF * humidity * humidity -
              1.99e-6f * tF * tF * humidity * humidity;
    } else {
        hiF = 0.5f * (tF + 61.0f + (tF - 68.0f) * 1.2f + humidity * 0.094f);
    }
    return (hiF - 32.0f) * 5.0f / 9.0f;
}

static void setStatusIndicators(int status) {
    switch (status) {
    case FIRE_MODEL_FIRE:
        digitalWrite(PIN_LED_STATUS, HIGH);
        if (g_consecutiveFire >= LOCAL_ALARM_CONSECUTIVE) {
            tone(PIN_BUZZER, 2400);
        }
        break;
    case FIRE_MODEL_WARNING:
        // Slow blink without blocking: derive phase from millis().
        digitalWrite(PIN_LED_STATUS, (millis() / 500) % 2 == 0 ? HIGH : LOW);
        noTone(PIN_BUZZER);
        break;
    default:
        digitalWrite(PIN_LED_STATUS, LOW);
        noTone(PIN_BUZZER);
        break;
    }
}

// ---------------------------------------------------------------------------
// Networking
// ---------------------------------------------------------------------------

static void ensureWifi() {
    if (WiFi.status() == WL_CONNECTED) {
        g_wifiBackoffMs = WIFI_RETRY_BASE_MS;
        return;
    }

    const uint32_t now = millis();
    if (now < g_nextWifiAttempt) {
        return;
    }

    Serial.printf("[wifi] connecting to %s\n", WIFI_SSID);
    WiFi.disconnect(true);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    const uint32_t deadline = millis() + WIFI_CONNECT_TIMEOUT_MS;
    while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
        delay(250);
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[wifi] connected, ip=%s\n", WiFi.localIP().toString().c_str());
        g_wifiBackoffMs = WIFI_RETRY_BASE_MS;
        g_nextWifiAttempt = 0;
    } else {
        // Exponential backoff, so a downed AP does not turn into a tight
        // reconnect loop that starves sensor sampling.
        Serial.printf("[wifi] failed, retrying in %u ms\n", g_wifiBackoffMs);
        g_nextWifiAttempt = millis() + g_wifiBackoffMs;
        g_wifiBackoffMs = min(g_wifiBackoffMs * 2, WIFI_RETRY_MAX_MS);
    }
}

static void ensureMqtt() {
    if (WiFi.status() != WL_CONNECTED || mqtt.connected()) {
        if (mqtt.connected()) {
            g_mqttBackoffMs = MQTT_RETRY_BASE_MS;
        }
        return;
    }

    const uint32_t now = millis();
    if (now < g_nextMqttAttempt) {
        return;
    }

    Serial.printf("[mqtt] connecting to %s:%d\n", MQTT_HOST, MQTT_PORT);

    // Last will: if this device drops off the network, the broker publishes
    // "offline" on its behalf so the dashboard notices immediately rather
    // than waiting for the backend's staleness sweep.
    const bool ok =
        (strlen(MQTT_USERNAME) > 0)
            ? mqtt.connect(DEVICE_ID, MQTT_USERNAME, MQTT_PASSWORD, g_statusTopic, 1,
                           true, "offline")
            : mqtt.connect(DEVICE_ID, g_statusTopic, 1, true, "offline");

    if (ok) {
        Serial.println("[mqtt] connected");
        mqtt.publish(g_statusTopic, "online", true);
        g_mqttBackoffMs = MQTT_RETRY_BASE_MS;
        g_nextMqttAttempt = 0;
    } else {
        Serial.printf("[mqtt] failed rc=%d, retrying in %u ms\n", mqtt.state(),
                      g_mqttBackoffMs);
        g_nextMqttAttempt = millis() + g_mqttBackoffMs;
        g_mqttBackoffMs = min(g_mqttBackoffMs * 2, MQTT_RETRY_MAX_MS);
    }
}

// ---------------------------------------------------------------------------
// Telemetry
// ---------------------------------------------------------------------------

static void publishTelemetry(const SensorReading &reading, int status,
                             float tempRate, float smokeRate, float heatIndex) {
    JsonDocument doc;
    doc["device_id"] = DEVICE_ID;
    doc["temperature_c"] = reading.temperatureC;
    doc["humidity_pct"] = reading.humidityPct;
    doc["smoke_ppm"] = reading.smokePpm;
    doc["air_quality_ppm"] = reading.airQualityPpm;
    doc["flame_analog_volts"] = reading.flameVolts;
    doc["flame_detected"] = reading.flameDetected ? 1 : 0;
    doc["device_status"] = fire_model_label(status);
    doc["firmware_version"] = FIREPROTECT_VERSION;
    doc["location"] = DEVICE_LOCATION;
    doc["device_uptime_s"] = millis() / 1000.0f;
    doc["temp_rate_c_per_min"] = tempRate;
    doc["smoke_rate_ppm_per_min"] = smokeRate;
    doc["heat_index_c"] = heatIndex;

    char payload[512];
    const size_t written = serializeJson(doc, payload, sizeof(payload));
    if (written == 0 || written >= sizeof(payload)) {
        Serial.println("[mqtt] payload serialisation failed; dropping sample");
        return;
    }

    if (mqtt.connected()) {
        if (!mqtt.publish(g_telemetryTopic, payload)) {
            Serial.println("[mqtt] publish failed");
        }
        if (status == FIRE_MODEL_FIRE && g_lastStatus != FIRE_MODEL_FIRE) {
            mqtt.publish(g_alertTopic, payload);
        }
    } else {
        Serial.println("[mqtt] offline; sample not published");
    }
}

static void mirrorToThingSpeak(const SensorReading &reading, int status) {
    if (strlen(THINGSPEAK_API_KEY) == 0) {
        return;
    }
    const uint32_t now = millis();
    if (now - g_lastThingSpeak < THINGSPEAK_INTERVAL_MS) {
        return;
    }
    g_lastThingSpeak = now;

    ThingSpeakEntry entry;
    entry.temperatureC = reading.temperatureC;
    entry.humidityPct = reading.humidityPct;
    entry.smokePpm = reading.smokePpm;
    entry.airQualityPpm = reading.airQualityPpm;
    entry.flameVolts = reading.flameVolts;
    entry.flameDetected = reading.flameDetected ? 1 : 0;
    entry.status = status;

    // Buffers when offline and drains on reconnect - see thingspeak_buffer.h.
    thingspeak.submit(entry, WiFi.status() == WL_CONNECTED);
}

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println();
    Serial.printf("FireProtect firmware %s starting (device %s)\n",
                  FIREPROTECT_VERSION, DEVICE_ID);

    pinMode(PIN_LED_STATUS, OUTPUT);
    pinMode(PIN_BUZZER, OUTPUT);
    pinMode(PIN_FLAME_DIGITAL, INPUT);
    digitalWrite(PIN_LED_STATUS, LOW);

    // 11 dB attenuation gives the full 0-3.3 V range on ADC1.
    analogSetAttenuation(ADC_11db);
    analogReadResolution(12);

    dht.begin();
    sensors.begin();
    thingspeak.begin(THINGSPEAK_API_KEY);

    snprintf(g_telemetryTopic, sizeof(g_telemetryTopic), "fireprotect/%s/telemetry",
             DEVICE_ID);
    snprintf(g_alertTopic, sizeof(g_alertTopic), "fireprotect/%s/alert", DEVICE_ID);
    snprintf(g_statusTopic, sizeof(g_statusTopic), "fireprotect/%s/status", DEVICE_ID);

    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setBufferSize(768);
    mqtt.setKeepAlive(60);

    ensureWifi();
    ensureMqtt();

    Serial.println("[boot] MQ sensors need ~60 s of preheat for stable readings");
}

void loop() {
    ensureWifi();
    ensureMqtt();
    mqtt.loop();

    const uint32_t now = millis();
    if (now - g_lastSensorRead < SENSOR_INTERVAL_MS) {
        setStatusIndicators(g_lastStatus);
        delay(10);
        return;
    }
    g_lastSensorRead = now;

    SensorReading reading;
    reading.temperatureC = dht.readTemperature();
    reading.humidityPct = dht.readHumidity();
    sensors.read(reading);

    // A failed DHT22 read returns NaN. Feeding that to the model would poison
    // every downstream comparison, so hold the previous value and flag it.
    if (!sensorReadingIsValid(reading)) {
        Serial.println("[sensor] invalid reading (NaN or out of range); skipping");
        if (g_havePrevious) {
            // Keep the last known-good status rather than silently going SAFE.
            setStatusIndicators(g_lastStatus);
        }
        return;
    }

    float tempRate = 0.0f;
    float smokeRate = 0.0f;
    if (g_havePrevious) {
        const float elapsedMin = (now - g_prevSampleMs) / 60000.0f;
        if (elapsedMin > 1e-6f) {
            tempRate = (reading.temperatureC - g_prevTemperature) / elapsedMin;
            smokeRate = (reading.smokePpm - g_prevSmoke) / elapsedMin;
        }
    }
    const float heatIndex = computeHeatIndex(reading.temperatureC, reading.humidityPct);

    // Feature order must match ml/generate_dataset.py FEATURE_COLUMNS.
    fire_feature_t features[FIRE_MODEL_N_FEATURES] = {
        reading.temperatureC,
        reading.humidityPct,
        reading.smokePpm,
        reading.airQualityPpm,
        reading.flameVolts,
        reading.flameDetected ? 1.0 : 0.0,
        tempRate,
        smokeRate,
        heatIndex,
    };
    const int status = fire_model_predict(features);

    if (status == FIRE_MODEL_FIRE) {
        g_consecutiveFire++;
    } else {
        g_consecutiveFire = 0;
    }

    Serial.printf("[read] T=%.1fC RH=%.1f%% smoke=%.0fppm air=%.0fppm flame=%.2fV -> %s\n",
                  reading.temperatureC, reading.humidityPct, reading.smokePpm,
                  reading.airQualityPpm, reading.flameVolts, fire_model_label(status));

    publishTelemetry(reading, status, tempRate, smokeRate, heatIndex);
    mirrorToThingSpeak(reading, status);
    setStatusIndicators(status);

    g_prevTemperature = reading.temperatureC;
    g_prevSmoke = reading.smokePpm;
    g_prevSampleMs = now;
    g_havePrevious = true;
    g_lastStatus = status;
}
