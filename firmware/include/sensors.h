/*
 * sensors.h - MQ-2 / MQ-135 / IR-flame acquisition and unit conversion.
 *
 * Kept header-only and free of Arduino globals where practical so the pure
 * conversion maths can be compiled and tested on the host (env:native_test).
 */

#ifndef FIREPROTECT_SENSORS_H
#define FIREPROTECT_SENSORS_H

#include <math.h>
#include <stdint.h>

#ifdef ARDUINO
#include <Arduino.h>
#endif

/** One complete sample from all four sensors, in physical units. */
struct SensorReading {
    float temperatureC = NAN;
    float humidityPct = NAN;
    float smokePpm = NAN;
    float airQualityPpm = NAN;
    float flameVolts = NAN;
    bool flameDetected = false;
};

/* -------------------------------------------------------------------------
 * Sensor characteristics - these must match ml/generate_dataset.py exactly,
 * or the on-device model sees a different feature distribution than it was
 * trained on.
 * ------------------------------------------------------------------------- */

static const float MQ2_CURVE_A = 574.25f;
static const float MQ2_CURVE_B = -2.222f;
static const float MQ135_CURVE_A = 116.6020682f;
static const float MQ135_CURVE_B = -2.769034857f;

/* Datasheet detection ceilings. Real MQ sensors pin at full scale rather than
 * reporting unbounded ppm; without these the log-log curve produces
 * physically impossible values near zero Rs/R0. */
static const float MQ2_MAX_PPM = 10000.0f;
static const float MQ135_MAX_PPM = 10000.0f;

/* Load resistor values on the sensor breakout boards, in kOhm. */
static const float MQ2_RL_KOHM = 5.0f;
static const float MQ135_RL_KOHM = 20.0f;

/* Clean-air resistance. Calibrate per unit - see docs/HARDWARE.md. */
static const float MQ2_R0_KOHM = 9.83f;
static const float MQ135_R0_KOHM = 76.63f;

static const float ADC_VREF = 3.3f;
static const int ADC_MAX_COUNTS = 4095;

/* The ESP32 SAR ADC is badly non-linear at both rails; only trust this band. */
static const float ADC_MIN_TRUSTED_V = 0.15f;
static const float ADC_MAX_TRUSTED_V = 3.10f;

/** Convert raw ADC counts to volts, clamped to the trustworthy band. */
static inline float adcCountsToVolts(int counts) {
    if (counts < 0) {
        counts = 0;
    }
    if (counts > ADC_MAX_COUNTS) {
        counts = ADC_MAX_COUNTS;
    }
    float volts = (static_cast<float>(counts) / ADC_MAX_COUNTS) * ADC_VREF;
    if (volts < ADC_MIN_TRUSTED_V) {
        volts = ADC_MIN_TRUSTED_V;
    }
    if (volts > ADC_MAX_TRUSTED_V) {
        volts = ADC_MAX_TRUSTED_V;
    }
    return volts;
}

/**
 * Sensor resistance from the divider: Rs = RL * (Vcc - Vout) / Vout.
 * Returns a large finite value rather than infinity when Vout approaches 0.
 */
static inline float mqSensorResistance(float volts, float loadKOhm) {
    if (volts < 0.01f) {
        return loadKOhm * 1000.0f;
    }
    return loadKOhm * (ADC_VREF - volts) / volts;
}

/** Datasheet power-law conversion, saturating at the detection ceiling. */
static inline float mqRatioToPpm(float ratio, float a, float b, float maxPpm) {
    if (!(ratio > 0.0f)) {
        return maxPpm;
    }
    const float ppm = a * powf(ratio, b);
    if (!isfinite(ppm) || ppm > maxPpm) {
        return maxPpm;
    }
    return ppm < 0.0f ? 0.0f : ppm;
}

/**
 * Reject a reading that cannot be physically real.
 *
 * A failed DHT22 read returns NaN, and a disconnected analogue sensor floats.
 * Both must be caught here rather than at the backend, so the local model
 * never classifies garbage.
 */
static inline bool sensorReadingIsValid(const SensorReading &r) {
    if (!isfinite(r.temperatureC) || !isfinite(r.humidityPct) ||
        !isfinite(r.smokePpm) || !isfinite(r.airQualityPpm) ||
        !isfinite(r.flameVolts)) {
        return false;
    }
    if (r.temperatureC < -40.0f || r.temperatureC > 300.0f) {
        return false;
    }
    if (r.humidityPct < 0.0f || r.humidityPct > 100.0f) {
        return false;
    }
    if (r.smokePpm < 0.0f || r.smokePpm > 100000.0f) {
        return false;
    }
    if (r.airQualityPpm < 0.0f || r.airQualityPpm > 100000.0f) {
        return false;
    }
    if (r.flameVolts < 0.0f || r.flameVolts > 3.3f) {
        return false;
    }
    return true;
}

#ifdef ARDUINO

/** Reads the three analogue sensors and the flame comparator. */
class SensorSuite {
  public:
    SensorSuite(uint8_t mq2Pin, uint8_t mq135Pin, uint8_t flameAnalogPin,
                uint8_t flameDigitalPin)
        : m_mq2Pin(mq2Pin), m_mq135Pin(mq135Pin), m_flameAnalogPin(flameAnalogPin),
          m_flameDigitalPin(flameDigitalPin) {}

    void begin() {
        pinMode(m_flameDigitalPin, INPUT);
    }

    /** Fill the sensor fields of ``out``. Does not touch temperature/humidity. */
    void read(SensorReading &out) const {
        const float mq2Volts = adcCountsToVolts(averageAnalog(m_mq2Pin));
        const float mq135Volts = adcCountsToVolts(averageAnalog(m_mq135Pin));

        const float rsMq2 = mqSensorResistance(mq2Volts, MQ2_RL_KOHM);
        const float rsMq135 = mqSensorResistance(mq135Volts, MQ135_RL_KOHM);

        out.smokePpm = mqRatioToPpm(rsMq2 / MQ2_R0_KOHM, MQ2_CURVE_A, MQ2_CURVE_B,
                                    MQ2_MAX_PPM);
        out.airQualityPpm = mqRatioToPpm(rsMq135 / MQ135_R0_KOHM, MQ135_CURVE_A,
                                         MQ135_CURVE_B, MQ135_MAX_PPM);

        out.flameVolts = adcCountsToVolts(averageAnalog(m_flameAnalogPin));
        // The breakout's comparator output is active-low.
        out.flameDetected = digitalRead(m_flameDigitalPin) == LOW;
    }

  private:
    /** Median-of-samples rejects the ESP32 ADC's occasional wild outliers. */
    static int averageAnalog(uint8_t pin) {
        const int kSamples = 9;
        int values[kSamples];
        for (int i = 0; i < kSamples; i++) {
            values[i] = analogRead(pin);
            delayMicroseconds(200);
        }
        for (int i = 1; i < kSamples; i++) {
            const int key = values[i];
            int j = i - 1;
            while (j >= 0 && values[j] > key) {
                values[j + 1] = values[j];
                j--;
            }
            values[j + 1] = key;
        }
        return values[kSamples / 2];
    }

    uint8_t m_mq2Pin;
    uint8_t m_mq135Pin;
    uint8_t m_flameAnalogPin;
    uint8_t m_flameDigitalPin;
};

#endif /* ARDUINO */

#endif /* FIREPROTECT_SENSORS_H */
