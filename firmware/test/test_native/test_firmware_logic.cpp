/*
 * Host-side tests for the firmware's pure logic.
 *
 * These compile and run without an ESP32: the model header, the MQ conversion
 * maths, the reading validator and the ThingSpeak ring buffer are all free of
 * Arduino dependencies. Run with:
 *
 *     cc/g++ -std=c++17 -I include test/test_native/test_firmware_logic.cpp
 *
 * or via `pio test -e native_test`.
 */

#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>

#include "fire_model.h"
#include "sensors.h"
#include "thingspeak_buffer.h"

static int g_checks = 0;
static int g_failures = 0;

#define CHECK(cond)                                                                \
    do {                                                                           \
        g_checks++;                                                                \
        if (!(cond)) {                                                             \
            g_failures++;                                                          \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);            \
        }                                                                          \
    } while (0)

/* ------------------------------------------------------------------------ */
/* Model header                                                             */
/* ------------------------------------------------------------------------ */

static void test_model_constants() {
    CHECK(FIRE_MODEL_N_FEATURES == 9);
    CHECK(FIRE_MODEL_N_CLASSES == 3);
    CHECK(FIRE_MODEL_SAFE == 0);
    CHECK(FIRE_MODEL_WARNING == 1);
    CHECK(FIRE_MODEL_FIRE == 2);
}

static void test_model_labels() {
    CHECK(std::strcmp(fire_model_label(FIRE_MODEL_SAFE), "SAFE") == 0);
    CHECK(std::strcmp(fire_model_label(FIRE_MODEL_WARNING), "WARNING") == 0);
    CHECK(std::strcmp(fire_model_label(FIRE_MODEL_FIRE), "FIRE") == 0);
    /* Out-of-range indices must not read past the array. */
    CHECK(std::strcmp(fire_model_label(-1), "UNKNOWN") == 0);
    CHECK(std::strcmp(fire_model_label(99), "UNKNOWN") == 0);
}

static void test_model_classifies_quiescent_as_safe() {
    /* temp, rh, smoke, air, flameV, flameDet, dTemp, dSmoke, heatIndex */
    fire_feature_t quiet[FIRE_MODEL_N_FEATURES] = {21.5, 45.0, 560.0, 110.0, 0.15,
                                                   0.0,  0.1,  0.5,   21.0};
    CHECK(fire_model_predict(quiet) == FIRE_MODEL_SAFE);
}

static void test_model_classifies_flashover_as_fire() {
    fire_feature_t fire[FIRE_MODEL_N_FEATURES] = {95.0, 15.0,  9500.0, 8000.0, 3.05,
                                                  1.0,  40.0, 1200.0, 110.0};
    CHECK(fire_model_predict(fire) == FIRE_MODEL_FIRE);
}

static void test_model_returns_valid_class_for_extremes() {
    /* However absurd the input, the tree must return a valid class index. */
    fire_feature_t lo[FIRE_MODEL_N_FEATURES] = {-40, 0, 0, 0, 0, 0, -999, -999, -40};
    fire_feature_t hi[FIRE_MODEL_N_FEATURES] = {300, 100, 1e5, 1e5, 3.3,
                                                1,   999, 9999, 300};
    const int a = fire_model_predict(lo);
    const int b = fire_model_predict(hi);
    CHECK(a >= 0 && a < FIRE_MODEL_N_CLASSES);
    CHECK(b >= 0 && b < FIRE_MODEL_N_CLASSES);
}

static void test_model_is_deterministic() {
    fire_feature_t f[FIRE_MODEL_N_FEATURES] = {50.0, 30.0, 1500.0, 400.0, 1.5,
                                               1.0,  10.0, 200.0,  55.0};
    const int first = fire_model_predict(f);
    for (int i = 0; i < 100; i++) {
        CHECK(fire_model_predict(f) == first);
    }
}

/* ------------------------------------------------------------------------ */
/* ADC and MQ conversion                                                    */
/* ------------------------------------------------------------------------ */

static void test_adc_conversion_clamps_to_trusted_band() {
    CHECK(adcCountsToVolts(0) == ADC_MIN_TRUSTED_V);
    CHECK(adcCountsToVolts(4095) == ADC_MAX_TRUSTED_V);
    /* Negative and over-range counts must not escape the band either. */
    CHECK(adcCountsToVolts(-100) == ADC_MIN_TRUSTED_V);
    CHECK(adcCountsToVolts(999999) == ADC_MAX_TRUSTED_V);
}

static void test_adc_conversion_is_monotonic() {
    float previous = -1.0f;
    for (int counts = 0; counts <= 4095; counts += 64) {
        const float volts = adcCountsToVolts(counts);
        CHECK(volts >= previous);
        previous = volts;
    }
}

static void test_sensor_resistance_handles_zero_volts() {
    const float r = mqSensorResistance(0.0f, MQ2_RL_KOHM);
    CHECK(std::isfinite(r));
    CHECK(r > 0.0f);
}

static void test_mq_curve_is_monotonic_decreasing() {
    float previous = 1e12f;
    for (float ratio = 0.1f; ratio <= 2.0f; ratio += 0.05f) {
        const float ppm = mqRatioToPpm(ratio, MQ2_CURVE_A, MQ2_CURVE_B, MQ2_MAX_PPM);
        CHECK(ppm <= previous);
        previous = ppm;
    }
}

static void test_mq_curve_saturates_at_ceiling() {
    /* A near-zero ratio would otherwise produce an absurd ppm value. */
    CHECK(mqRatioToPpm(1e-6f, MQ2_CURVE_A, MQ2_CURVE_B, MQ2_MAX_PPM) == MQ2_MAX_PPM);
    CHECK(mqRatioToPpm(0.0f, MQ2_CURVE_A, MQ2_CURVE_B, MQ2_MAX_PPM) == MQ2_MAX_PPM);
    CHECK(mqRatioToPpm(-1.0f, MQ2_CURVE_A, MQ2_CURVE_B, MQ2_MAX_PPM) == MQ2_MAX_PPM);
}

static void test_mq_curve_matches_python_at_unity_ratio() {
    /* At Rs/R0 = 1 the curve must equal coefficient a, matching
     * ml/generate_dataset.py's mq_ratio_to_ppm. */
    const float ppm = mqRatioToPpm(1.0f, MQ2_CURVE_A, MQ2_CURVE_B, MQ2_MAX_PPM);
    CHECK(std::fabs(ppm - MQ2_CURVE_A) < 0.01f);
}

/* ------------------------------------------------------------------------ */
/* Reading validation - the NaN guard                                       */
/* ------------------------------------------------------------------------ */

static SensorReading goodReading() {
    SensorReading r;
    r.temperatureC = 22.0f;
    r.humidityPct = 45.0f;
    r.smokePpm = 560.0f;
    r.airQualityPpm = 110.0f;
    r.flameVolts = 0.15f;
    r.flameDetected = false;
    return r;
}

static void test_valid_reading_accepted() {
    CHECK(sensorReadingIsValid(goodReading()));
}

static void test_nan_temperature_rejected() {
    /* This is exactly what a failed DHT22 read produces. */
    SensorReading r = goodReading();
    r.temperatureC = NAN;
    CHECK(!sensorReadingIsValid(r));
}

static void test_nan_humidity_rejected() {
    SensorReading r = goodReading();
    r.humidityPct = NAN;
    CHECK(!sensorReadingIsValid(r));
}

static void test_infinite_values_rejected() {
    SensorReading r = goodReading();
    r.smokePpm = INFINITY;
    CHECK(!sensorReadingIsValid(r));
    r = goodReading();
    r.airQualityPpm = -INFINITY;
    CHECK(!sensorReadingIsValid(r));
}

static void test_out_of_range_values_rejected() {
    SensorReading r = goodReading();
    r.temperatureC = 5000.0f;
    CHECK(!sensorReadingIsValid(r));
    r = goodReading();
    r.humidityPct = 150.0f;
    CHECK(!sensorReadingIsValid(r));
    r = goodReading();
    r.flameVolts = 9.0f;
    CHECK(!sensorReadingIsValid(r));
    r = goodReading();
    r.smokePpm = -1.0f;
    CHECK(!sensorReadingIsValid(r));
}

/* ------------------------------------------------------------------------ */
/* ThingSpeak ring buffer                                                   */
/* ------------------------------------------------------------------------ */

static ThingSpeakEntry entryWith(float temp) {
    ThingSpeakEntry e;
    e.temperatureC = temp;
    return e;
}

static void test_buffer_starts_empty() {
    ThingSpeakBuffer buffer;
    buffer.begin("KEY");
    CHECK(buffer.pending() == 0);
    ThingSpeakEntry out;
    CHECK(!buffer.pop(out));
}

static void test_buffer_is_fifo() {
    ThingSpeakBuffer buffer;
    buffer.begin("KEY");
    buffer.push(entryWith(1.0f));
    buffer.push(entryWith(2.0f));
    buffer.push(entryWith(3.0f));
    CHECK(buffer.pending() == 3);

    ThingSpeakEntry out;
    CHECK(buffer.pop(out) && out.temperatureC == 1.0f);
    CHECK(buffer.pop(out) && out.temperatureC == 2.0f);
    CHECK(buffer.pop(out) && out.temperatureC == 3.0f);
    CHECK(buffer.pending() == 0);
}

static void test_buffer_overwrites_oldest_when_full() {
    ThingSpeakBuffer buffer;
    buffer.begin("KEY");
    for (int i = 0; i < THINGSPEAK_BUFFER_CAPACITY + 10; i++) {
        buffer.push(entryWith(static_cast<float>(i)));
    }
    CHECK(buffer.pending() == THINGSPEAK_BUFFER_CAPACITY);
    CHECK(buffer.dropped() == 10);

    /* The oldest 10 are gone; the head is now entry #10. */
    ThingSpeakEntry out;
    CHECK(buffer.peek(out) && out.temperatureC == 10.0f);
}

static void test_buffer_wraps_correctly() {
    ThingSpeakBuffer buffer;
    buffer.begin("KEY");
    ThingSpeakEntry out;
    /* Cycle through the ring several times to catch index arithmetic bugs. */
    for (int cycle = 0; cycle < 5; cycle++) {
        for (int i = 0; i < THINGSPEAK_BUFFER_CAPACITY; i++) {
            buffer.push(entryWith(static_cast<float>(i)));
        }
        for (int i = 0; i < THINGSPEAK_BUFFER_CAPACITY; i++) {
            CHECK(buffer.pop(out) && out.temperatureC == static_cast<float>(i));
        }
        CHECK(buffer.pending() == 0);
    }
    CHECK(buffer.dropped() == 0);
}

static void test_buffer_peek_does_not_remove() {
    ThingSpeakBuffer buffer;
    buffer.begin("KEY");
    buffer.push(entryWith(7.0f));
    ThingSpeakEntry out;
    CHECK(buffer.peek(out) && out.temperatureC == 7.0f);
    CHECK(buffer.pending() == 1);
    CHECK(buffer.peek(out) && out.temperatureC == 7.0f);
}

static void test_buffer_survives_offline_period() {
    /* Simulates a Wi-Fi outage: submit while offline, nothing is sent. */
    ThingSpeakBuffer buffer;
    buffer.begin("KEY");
    for (int i = 0; i < 20; i++) {
        buffer.submit(entryWith(static_cast<float>(i)), /*online=*/false);
    }
    CHECK(buffer.pending() == 20);
    CHECK(buffer.sent() == 0);
}

/* ------------------------------------------------------------------------ */

int main() {
    test_model_constants();
    test_model_labels();
    test_model_classifies_quiescent_as_safe();
    test_model_classifies_flashover_as_fire();
    test_model_returns_valid_class_for_extremes();
    test_model_is_deterministic();

    test_adc_conversion_clamps_to_trusted_band();
    test_adc_conversion_is_monotonic();
    test_sensor_resistance_handles_zero_volts();
    test_mq_curve_is_monotonic_decreasing();
    test_mq_curve_saturates_at_ceiling();
    test_mq_curve_matches_python_at_unity_ratio();

    test_valid_reading_accepted();
    test_nan_temperature_rejected();
    test_nan_humidity_rejected();
    test_infinite_values_rejected();
    test_out_of_range_values_rejected();

    test_buffer_starts_empty();
    test_buffer_is_fifo();
    test_buffer_overwrites_oldest_when_full();
    test_buffer_wraps_correctly();
    test_buffer_peek_does_not_remove();
    test_buffer_survives_offline_period();

    std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
