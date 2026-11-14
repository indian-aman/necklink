/*
 * ESP32CAM_Looming_Avoidance.ino - NeckSight v2.0.0 ESP32-CAM Reflex Sketch
 *
 * Real-time monocular collision & thin-obstacle (pole/wire) avoidance reflex:
 *   - Captures OV2640 QVGA (320x240) 8-bit grayscale frames (`PIXFORMAT_GRAYSCALE`,
 *     `FRAMESIZE_QVGA`) on AI-Thinker ESP32-CAM when `esp_camera.h` is present,
 *     or synthesizes deterministic 32x32 looming pole/disc test frames when built
 *     headless (e.g., `pio ci --board=esp32dev` without `esp32-camera`).
 *   - Applies optional 3-DOF `(yaw, pitch, roll)` IMU gyro derotation in pure
 *     integer BAM/Q4 math before stepping the 32x32 Optic Lobe engine.
 *   - Asserts hardware E-stop (`PIN_ESTOP`) and streams `DNp01` brake + `DNa02`
 *     directional avoidance `(avoid_x_q7, avoid_y_q7)` over UART.
 *
 * Zero heap allocation in NeckSight (`sizeof(necksight_state_t) = 14,624 B`),
 * zero floating-point instructions.
 *
 * Author: Amandeep Singh Bains
 * License: MIT
 */

#include <Arduino.h>
#include <NeckSight.h>

#if defined(ESP32) && defined(__has_include)
#  if __has_include("esp_camera.h")
#    define NECKSIGHT_HAS_ESP_CAMERA 1
#    include "esp_camera.h"
#  endif
#endif

#ifndef PIN_ESTOP
#define PIN_ESTOP 4
#endif

/* AI-Thinker ESP32-CAM OV2640 pin map */
#if defined(NECKSIGHT_HAS_ESP_CAMERA)
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
#endif

/* Single static NeckSight instance (~14.3 KB SRAM, zero heap) */
static NeckSight g_sight;
static uint32_t g_last_ms = 0U;
static bool g_cam_ready = false;

#if !defined(NECKSIGHT_HAS_ESP_CAMERA)
static uint8_t g_sim_grid[NECKSIGHT_PIXELS];
static uint32_t g_sim_tick = 0U;
#endif

/* Hook for 6-axis IMU (e.g., ICM-42688 / BMI270 / MPU6050) in milliradians/sec. */
static void readImuRatesMradS(
    int32_t *yaw_mrad_s,
    int32_t *pitch_mrad_s,
    int32_t *roll_mrad_s
) {
    *yaw_mrad_s = 0;
    *pitch_mrad_s = 0;
    *roll_mrad_s = 0;
}

#if defined(NECKSIGHT_HAS_ESP_CAMERA)
static bool initEspCameraQvgaGray() {
    camera_config_t cfg = {};
    cfg.ledc_channel = LEDC_CHANNEL_0;
    cfg.ledc_timer = LEDC_TIMER_0;
    cfg.pin_d0 = Y2_GPIO_NUM;
    cfg.pin_d1 = Y3_GPIO_NUM;
    cfg.pin_d2 = Y4_GPIO_NUM;
    cfg.pin_d3 = Y5_GPIO_NUM;
    cfg.pin_d4 = Y6_GPIO_NUM;
    cfg.pin_d5 = Y7_GPIO_NUM;
    cfg.pin_d6 = Y8_GPIO_NUM;
    cfg.pin_d7 = Y9_GPIO_NUM;
    cfg.pin_xclk = XCLK_GPIO_NUM;
    cfg.pin_pclk = PCLK_GPIO_NUM;
    cfg.pin_vsync = VSYNC_GPIO_NUM;
    cfg.pin_href = HREF_GPIO_NUM;
    cfg.pin_sccb_sda = SIOD_GPIO_NUM;
    cfg.pin_sccb_scl = SIOC_GPIO_NUM;
    cfg.pin_pwdn = PWDN_GPIO_NUM;
    cfg.pin_reset = RESET_GPIO_NUM;
    cfg.xclk_freq_hz = 20000000;
    cfg.pixel_format = PIXFORMAT_GRAYSCALE;
    cfg.frame_size = FRAMESIZE_QVGA; /* 320x240 raw grayscale */
    cfg.jpeg_quality = 12;
    cfg.fb_count = 2;
    return (esp_camera_init(&cfg) == ESP_OK);
}
#endif

void setup() {
    pinMode(PIN_ESTOP, OUTPUT);
    digitalWrite(PIN_ESTOP, LOW);

    Serial.begin(115200);
    delay(150);

    g_sight.begin(NECKSIGHT_DEFAULT_THRESHOLD_Q10);
    g_sight.setLC4BarChannel(true, NECKSIGHT_DEFAULT_LC4_WEIGHT_Q8);
    g_sight.setT5OffGainQ8(NECKSIGHT_DEFAULT_T5_GAIN_Q8);

#if defined(NECKSIGHT_HAS_ESP_CAMERA)
    g_cam_ready = initEspCameraQvgaGray();
#else
    g_cam_ready = false;
#endif

    Serial.print(F("NeckSight v"));
    Serial.print(F(NECKSIGHT_VERSION_STRING));
    Serial.print(F(" state_bytes="));
    Serial.print((unsigned long)sizeof(necksight_state_t));
    Serial.print(F(" cam="));
    Serial.println(g_cam_ready ? F("OV2640_QVGA") : F("SYNTH_FALLBACK"));

    g_last_ms = millis();
}

void loop() {
    uint32_t now_ms = millis();
    uint16_t dt_ms = (uint16_t)((now_ms > g_last_ms) ? (now_ms - g_last_ms) : 16U);
    if (dt_ms == 0U || dt_ms > 250U) {
        dt_ms = 16U;
    }
    g_last_ms = now_ms;

    /* 1. Optional 3-DOF IMU ego-motion derotation (yaw, pitch, roll) */
    int32_t yaw_mrad_s = 0, pitch_mrad_s = 0, roll_mrad_s = 0;
    readImuRatesMradS(&yaw_mrad_s, &pitch_mrad_s, &roll_mrad_s);
    if (yaw_mrad_s != 0 || pitch_mrad_s != 0 || roll_mrad_s != 0) {
        /* focal_px_q4 = 365 matches ~70 deg HFOV on the 32-px grid */
        g_sight.derotateIMU(yaw_mrad_s, pitch_mrad_s, roll_mrad_s, dt_ms, 365U);
    }

    /* 2. Step NeckSight on live OV2640 QVGA grayscale or synthetic fallback */
#if defined(NECKSIGHT_HAS_ESP_CAMERA)
    if (g_cam_ready) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb != NULL) {
            g_sight.stepFrameU8(
                fb->buf,
                (uint16_t)fb->height,
                (uint16_t)fb->width,
                (uint16_t)fb->width,
                dt_ms
            );
            esp_camera_fb_return(fb);
        }
    }
#else
    {
        /* Deterministic synthetic left-side thin vertical pole approach cycle */
        uint32_t phase = g_sim_tick % 28U;
        if (phase == 0U) {
            g_sight.reset();
        }
        int32_t half_w = 1 + (int32_t)((phase * 5U) / 27U);
        for (uint16_t y = 0U; y < NECKSIGHT_GRID; ++y) {
            for (uint16_t x = 0U; x < NECKSIGHT_GRID; ++x) {
                int32_t dx = (int32_t)x - 10;
                if (dx < 0) dx = -dx;
                g_sim_grid[y * NECKSIGHT_GRID + x] = (dx <= half_w) ? 20U : 205U;
            }
        }
        g_sight.stepGridU8(g_sim_grid, 16U);
        ++g_sim_tick;
        delay(16);
    }
#endif

    /* 3. Actuate E-stop GPIO & emit directional avoidance command */
    const necksight_result_t &r = g_sight.lastResult();
    digitalWrite(PIN_ESTOP, r.triggered ? HIGH : LOW);

    if (r.triggered) {
        Serial.print(F("AVOID trig=1 loom="));
        Serial.print((unsigned)r.looming_score_q10);
        Serial.print(F(" lc4="));
        Serial.print((unsigned)r.lc4_score_q10);
        Serial.print(F(" ttc_ms="));
        Serial.print((unsigned)r.est_ttc_ms);
        Serial.print(F(" foe=("));
        Serial.print((unsigned)r.focus_x);
        Serial.print(F(","));
        Serial.print((unsigned)r.focus_y);
        Serial.print(F(") dodge_q7=("));
        Serial.print((int)r.avoid_x_q7);
        Serial.print(F(","));
        Serial.print((int)r.avoid_y_q7);
        Serial.println(F(")"));
    }
}
