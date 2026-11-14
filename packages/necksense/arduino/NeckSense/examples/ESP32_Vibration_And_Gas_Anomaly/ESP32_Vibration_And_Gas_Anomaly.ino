/*
 * ESP32_Vibration_And_Gas_Anomaly.ino - NeckSense v2.0.0 Arduino Example
 *
 * Demonstrates zero-heap, zero-floating-point 1-shot edge sensory intelligence:
 *   1. 3-axis accelerometer (`MPU6050`/`LSM6DSO`/`ADXL345`, 128 samples in milli-g)
 *      -> 16-ch Chordotonal vibration extractor (`ns_vib_extract_3axis_i16`)
 *      -> Antennal Lobe load-invariant gain normalization (`ns_antennal_normalize`)
 *      -> 1-shot Mushroom Body motor/bearing fault classifier + novelty detector
 *         (invariant across 10x throttle / motor-load changes, strips 1g tilt!)
 *   2. 8-channel BME688 / MOX gas array -> Antennal Lobe drift + plume whiff rate
 *      (`ns_antennal_step_whiff`) -> 1-shot chemical leak anomaly detection.
 *
 * For < 6 KB total SRAM on STM32G0 / ESP8266 / ATmega4809, pass `-DNS_TINY=1`
 * in `platformio.ini` (`build_flags = -DNS_TINY=1`; auto-enabled on AVR/ESP8266).
 *
 * Copyright (c) 2026 Amandeep Singh Bains
 * SPDX-License-Identifier: MIT
 */

#include <Arduino.h>
#include <NeckSense.h>

enum {
    DEMO_KC      = NS_MAX_KC,
    DEMO_WORDS   = NS_MAX_WORDS,
    DEMO_K_CLASS = (NS_MAX_KC == 512u) ? 26u : 51u,
    DEMO_K_NOV   = (NS_MAX_KC == 512u) ? 10u : 20u
};

/* Static workspaces: ~20 KB default, or 5,804 B (< 6 KB) with -DNS_TINY=1 */
static ns_vib_fe_t   g_vib;
static ns_antennal_t g_vib_ant;
static ns_kenyon_t   g_vib_ken;
static ns_novelty_t  g_vib_nov;
static ns_valence_t  g_vib_clf;

static ns_antennal_t g_gas_ant;
static ns_kenyon_t   g_gas_ken;
static ns_novelty_t  g_gas_nov;

/* 32-entry integer sine LUT in milli-g [-512 .. +512] for self-contained demo */
static const int16_t k_sin32_mg[32] = {
      0, 100, 196, 285, 362, 426, 473, 502,
    512, 502, 473, 426, 362, 285, 196, 100,
      0,-100,-196,-285,-362,-426,-473,-502,
   -512,-502,-473,-426,-362,-285,-196,-100
};

/* Replace with I2C FIFO burst read from MPU6050 / LSM6DSO / ADXL345 in milli-g */
static void read_imu_window_3axis_mg(uint32_t fault_mode, int32_t load_mult, int16_t *xyz_out) {
    for (uint32_t t = 0u; t < 128u; ++t) {
        int32_t vx = ((int32_t)k_sin32_mg[t & 31u] * load_mult) >> 1;
        int32_t vy = ((int32_t)k_sin32_mg[(t + 8u) & 31u] * load_mult) >> 2;
        int32_t vz = 1000; /* +1000 mg (1g) static gravity on Z-axis */

        if (fault_mode == 1u) {
            /* Outer-race bearing spall: periodic damped HF impact bursts on Z */
            uint32_t m = t % 16u;
            int32_t imp = (m == 0u) ? 900 : ((m == 1u) ? -650 : ((m == 2u) ? 350 : 0));
            vz += (imp * load_mult) >> 1;
        } else if (fault_mode == 2u) {
            /* Shaft misalignment: strong 2X + 3X radial harmonics */
            vx += ((int32_t)k_sin32_mg[(t * 2u) & 31u] * load_mult);
            vy += ((int32_t)k_sin32_mg[(t * 3u) & 31u] * load_mult);
        }

        xyz_out[3u * t]      = (int16_t)vx;
        xyz_out[3u * t + 1u] = (int16_t)vy;
        xyz_out[3u * t + 2u] = (int16_t)vz;
    }
}

static void learn_vibration_state(uint32_t class_id, uint32_t fault_mode) {
    int16_t imu_xyz[128u * 3u];
    int32_t feat_q16[NS_VIB_CHANNELS];
    int32_t norm_q16[NS_VIB_CHANNELS];
    uint64_t kc_tag[DEMO_WORDS];

    read_imu_window_3axis_mg(fault_mode, 2 /* 1x reference load */, imu_xyz);
    ns_vib_extract_3axis_i16(&g_vib, imu_xyz, 128u, feat_q16);
    ns_antennal_normalize(&g_vib_ant, feat_q16, norm_q16);
    ns_kenyon_hash(&g_vib_ken, norm_q16, kc_tag);
    ns_valence_learn(&g_vib_clf, kc_tag, class_id, NS_Q16_ONE);
    if (fault_mode == 0u) {
        /* Habituate MBON-alpha'3 novelty detector to healthy motor baseline */
        for (uint32_t k = 0u; k < 4u; ++k) {
            ns_novelty_update_and_score(&g_vib_nov, kc_tag);
        }
    }
}

void setup() {
    Serial.begin(115200);
    delay(200);

    size_t suite_bytes = sizeof(ns_antennal_t) + sizeof(ns_kenyon_t) +
                         sizeof(ns_novelty_t) + sizeof(ns_valence_t) + sizeof(ns_vib_fe_t);

    Serial.println(F("NeckSense v2.0.0 - Zero-Float Vibration + Gas Edge AI"));
    Serial.print(F("Core 5-struct SRAM footprint (B): "));
    Serial.println((unsigned long)suite_bytes);

    /* 1. Initialize Vibration Pipeline (16-ch Chordotonal + Antennal + MB) */
    ns_vib_init(&g_vib, 128u, 5u);
    ns_antennal_init(&g_vib_ant, NS_VIB_CHANNELS, 256, NS_Q16_ONE, 2u, 655, 1311, 32768, 22938);
    ns_kenyon_init(&g_vib_ken, NS_VIB_CHANNELS, DEMO_KC, 6u, DEMO_K_CLASS, 42u);
    ns_novelty_init(&g_vib_nov, DEMO_KC, 36045 /* lr=0.55 */, 16);
    ns_valence_init(&g_vib_clf, DEMO_KC, 3u, 6554, 9830);

    /* 1-shot calibration at 1x load + consolidate into KC-alpha/beta LTM */
    learn_vibration_state(0u, 0u); /* class 0: healthy */
    learn_vibration_state(1u, 1u); /* class 1: bearing spall */
    learn_vibration_state(2u, 2u); /* class 2: shaft misalignment */
    ns_valence_consolidate(&g_vib_clf, 16384);

    /* 2. Initialize 8-channel MOX / BME688 Gas Pipeline */
    ns_antennal_init(&g_gas_ant, 8u, 655, NS_Q16_ONE, 2u, 655, 1311, 32768, 22938);
    ns_kenyon_init(&g_gas_ken, 8u, DEMO_KC, 6u, DEMO_K_NOV, 99u);
    ns_novelty_init(&g_gas_nov, DEMO_KC, 36045, 32);
}

static uint32_t g_tick = 0u;

void loop() {
    int16_t imu_xyz[128u * 3u];
    int32_t vib_feat[NS_VIB_CHANNELS];
    int32_t vib_norm[NS_VIB_CHANNELS];
    uint64_t vib_tag[DEMO_WORDS];
    uint32_t pred_class = 0u;
    int32_t pred_score_q16 = 0;
    int32_t vib_nov_q16;

    /* Cycle through healthy (1x), healthy (8x high load), and bearing spall (8x load) */
    uint32_t mode = (g_tick % 6u == 4u) ? 1u : ((g_tick % 6u == 5u) ? 2u : 0u);
    int32_t load_mult = (g_tick & 1u) ? 16 /* 8x load */ : 2 /* 1x load */;

    read_imu_window_3axis_mg(mode, load_mult, imu_xyz);
    ns_vib_extract_3axis_i16(&g_vib, imu_xyz, 128u, vib_feat);
    ns_antennal_normalize(&g_vib_ant, vib_feat, vib_norm);
    ns_kenyon_hash(&g_vib_ken, vib_norm, vib_tag);
    vib_nov_q16 = ns_novelty_score(&g_vib_nov, vib_tag);
    ns_valence_predict(&g_vib_clf, vib_tag, &pred_class, &pred_score_q16);

    /* Simulated 8-ch MOX gas tick with intermittent plume whiffs */
    int32_t gas_raw_q16[8] = {8000, 6000, 5000, 4000, 7000, 5500, 4500, 6200};
    if ((g_tick % 5u) == 0u) {
        gas_raw_q16[0] += 42000;
        gas_raw_q16[4] += 31000;
    }
    int32_t gas_norm_q16[8];
    int32_t whiff_rate_q16 = 0;
    uint64_t gas_tag[DEMO_WORDS];
    ns_antennal_step_whiff(&g_gas_ant, gas_raw_q16, gas_norm_q16, &whiff_rate_q16);
    ns_kenyon_hash(&g_gas_ken, gas_norm_q16, gas_tag);
    int32_t gas_nov_q16 = ns_novelty_update_and_score(&g_gas_nov, gas_tag);

    Serial.print(F("tick="));
    Serial.print((unsigned long)g_tick);
    Serial.print(F(" vib_mode="));
    Serial.print((unsigned long)mode);
    Serial.print(F(" pred="));
    Serial.print((unsigned long)pred_class);
    Serial.print(F(" vib_nov_permille="));
    Serial.print((long)((vib_nov_q16 * 1000L) >> 16));
    Serial.print(F(" gas_nov_permille="));
    Serial.print((long)((gas_nov_q16 * 1000L) >> 16));
    Serial.print(F(" whiff_permille="));
    Serial.println((long)((whiff_rate_q16 * 1000L) >> 16));

    ++g_tick;
    delay(500);
}
