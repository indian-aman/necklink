/*
 * ESP32_9DOF_SelfCalibrating_Compass.ino
 *
 * Demonstrates NeckCompass v2.0.0 on ESP32 / Arduino:
 *   1. 9-Axis 3D tilt-compensated IMU + Magnetometer front-end (wave-slam gated)
 *   2. Self-supervised R-neuron -> E-PG online soft-iron calibration (1,104 B RAM, 0 floats)
 *   3. CPU4 vector path integration + PFL3/PFL2 GPS-denied return-to-home steering (264 B RAM)
 *
 * Replace readImu9DOF() with your ICM-20948, BNO085 (raw mode), LSM9DS1, or MPU9250/QMC5883L
 * I2C/SPI burst read. No figure-8 calibration routine or heap allocation required.
 */

#include <Arduino.h>
#include <NeckCompass.h>

static NeckCompassPlasticRing compass;
static NeckCompassNavigator   navigator;
static uint32_t               last_step_ms = 0;

/* Replace with hardware sensor reads in centideg/s, milli-g, and raw/mG magnetometer units */
static void readImu9DOF(
    int32_t& gx_cdeg_s, int32_t& gy_cdeg_s, int32_t& gz_cdeg_s,
    int32_t& ax_mg, int32_t& ay_mg, int32_t& az_mg,
    int32_t& mx, int32_t& my, int32_t& mz
) {
    gx_cdeg_s = 120;    /* +1.20 deg/s roll rate */
    gy_cdeg_s = -80;    /* -0.80 deg/s pitch rate */
    gz_cdeg_s = 1500;   /* +15.00 deg/s yaw rate */
    ax_mg     = -173;   /* ~10 deg nose-up pitch */
    ay_mg     = 254;    /* ~15 deg starboard bank */
    az_mg     = 951;    /* ~1.00 g total gravity norm */
    mx        = 21500;  /* arbitrary consistent magnetometer units */
    my        = -6400;
    mz        = -43200; /* steep downward geomagnetic dip component */
}

void setup() {
    Serial.begin(115200);
    compass.begin(500u);       /* 500 ms complementary gravity time constant */
    navigator.begin(1500);     /* 1.50 m arrival radius taper */
    last_step_ms = millis();

    Serial.println(F("NeckCompass v2.0.0 -- 9-DOF Self-Calibrating Compass + GPS-Denied Homing"));
}

void loop() {
    const uint32_t now_ms = millis();
    const uint32_t dt_ms  = now_ms - last_step_ms;
    if (dt_ms < 20u) {
        return; /* Run deterministic 50 Hz navigation loop */
    }
    last_step_ms = now_ms;

    int32_t gx, gy, gz, ax, ay, az, mx, my, mz;
    readImu9DOF(gx, gy, gz, ax, ay, az, mx, my, mz);

    /* 1. Tilt-compensate 3D mag + learn local steel-hull / harbor warp online */
    const int32_t heading_cdeg = compass.update9DOF_cdeg(
        gx, gy, gz,
        ax, ay, az,
        mx, my, mz,
        dt_ms,
        true /* self-supervised Hebbian/anti-Hebbian R -> E-PG learning */
    );

    /* 2. Integrate forward vessel/rover speed (e.g. 1,400 mm/s = 1.4 m/s) in CPU4 columns */
    const int32_t speed_mm_s = 1400;
    navigator.update(speed_mm_s, dt_ms, compass.headingBam());

    /* 3. Compute PFL3 + PFL2 return-to-home yaw-rate command (bounded to +/-45.00 deg/s) */
    const int32_t turn_cmd_cdeg_s = navigator.steeringCmdCentiDegS(
        compass.headingBam(),
        4500
    );

    Serial.print(F("heading_deg="));
    Serial.print(heading_cdeg * 0.01f, 2);
    Serial.print(F(" roll_deg="));
    Serial.print(compass.rollDeg(), 2);
    Serial.print(F(" pitch_deg="));
    Serial.print(compass.pitchDeg(), 2);
    Serial.print(F(" home_m="));
    Serial.print(navigator.homeDistanceMeters(), 2);
    Serial.print(F(" turn_deg_s="));
    Serial.println(turn_cmd_cdeg_s * 0.01f, 2);
}
