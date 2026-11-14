#ifndef NECKCOMPASS_ARDUINO_HPP
#define NECKCOMPASS_ARDUINO_HPP

/*
 * NeckCompass v3.0.0 -- C++ / Arduino Header Wrapper
 *
 * Thin, zero-heap C++11 wrapper around the zero-float ANSI C99 BANC v888 Central Complex
 * core (nc_tilt_t, nc_compass_t, nc_plastic_ring_t, nc_navigator_t).
 * Works on ESP32, STM32, RP2040, Teensy, nRF52, and AVR targets.
 */

#include "csrc/neckcompass.h"

#ifdef __cplusplus

class NeckCompassTilt {
public:
    explicit NeckCompassTilt(uint16_t tau_ms = 500u) {
        begin(tau_ms);
    }

    void begin(uint16_t tau_ms = 500u) {
        nc_tilt_init(&tilt_);
        tilt_.tau_ms = tau_ms;
    }

    void resetCentiDeg(int32_t roll_cdeg = 0, int32_t pitch_cdeg = 0) {
        nc_tilt_reset(&tilt_, nc_cdeg_to_bam(roll_cdeg), nc_cdeg_to_bam(pitch_cdeg));
    }

    int32_t update9DOF_cdeg(
        int32_t gx_cdeg_s, int32_t gy_cdeg_s, int32_t gz_cdeg_s,
        int32_t ax_mg, int32_t ay_mg, int32_t az_mg,
        int32_t mx, int32_t my, int32_t mz,
        uint32_t dt_ms,
        int32_t* out_yaw_rate_bam_s = nullptr
    ) {
        nc_bam_t mag_bam = nc_tilt_update_9dof(
            &tilt_,
            nc_cdeg_s_to_bam_s(gx_cdeg_s),
            nc_cdeg_s_to_bam_s(gy_cdeg_s),
            nc_cdeg_s_to_bam_s(gz_cdeg_s),
            ax_mg, ay_mg, az_mg,
            mx, my, mz,
            dt_ms,
            out_yaw_rate_bam_s
        );
        return nc_bam_to_cdeg(mag_bam);
    }

    int32_t rollCentiDeg() const { return nc_bam_to_cdeg(nc_tilt_roll_bam(&tilt_)); }
    int32_t pitchCentiDeg() const { return nc_bam_to_cdeg(nc_tilt_pitch_bam(&tilt_)); }
    float rollDeg() const { return 0.01f * static_cast<float>(rollCentiDeg()); }
    float pitchDeg() const { return 0.01f * static_cast<float>(pitchCentiDeg()); }
    bool accelGated() const { return tilt_.accel_gated != 0u; }

    nc_tilt_t& raw() { return tilt_; }
    const nc_tilt_t& raw() const { return tilt_; }

private:
    nc_tilt_t tilt_;
};

class NeckCompassRing {
public:
    NeckCompassRing() {
        nc_compass_init(&comp_);
        nc_tilt_init(&tilt_);
    }

    void begin(int32_t cue_noise_cdeg = 300, uint16_t reanchor_ms = 10000u, uint16_t tilt_tau_ms = 500u) {
        nc_compass_init(&comp_);
        nc_compass_set_cue_noise(&comp_, cue_noise_cdeg);
        nc_compass_set_reanchor_ms(&comp_, reanchor_ms);
        nc_tilt_init(&tilt_);
        tilt_.tau_ms = tilt_tau_ms;
    }

    void resetCentiDeg(int32_t heading_cdeg = 0) {
        nc_compass_reset(&comp_, nc_cdeg_to_bam(heading_cdeg));
    }

    void useBancConnectome() {
        nc_compass_set_weights(&comp_, nc_banc_default_weights());
    }

    void useAnalyticV2() {
        nc_compass_set_weights(&comp_, nullptr);
    }

    int16_t coherenceQ15() const {
        return nc_compass_coherence_q15(&comp_);
    }

    int32_t update2D_cdeg(
        int32_t omega_cdeg_s,
        uint32_t dt_ms,
        int32_t mag_heading_cdeg,
        bool mag_valid = true,
        int32_t sun_heading_cdeg = 0,
        bool sun_valid = false
    ) {
        nc_bam_t h = nc_compass_update(
            &comp_,
            nc_cdeg_s_to_bam_s(omega_cdeg_s),
            dt_ms,
            nc_cdeg_to_bam(mag_heading_cdeg),
            mag_valid ? 1u : 0u,
            NC_Q15_ONE,
            nc_cdeg_to_bam(sun_heading_cdeg),
            sun_valid ? 1u : 0u,
            sun_valid ? NC_Q15_ONE : 0
        );
        return nc_bam_to_cdeg(h);
    }

    int32_t update9DOF_cdeg(
        int32_t gx_cdeg_s, int32_t gy_cdeg_s, int32_t gz_cdeg_s,
        int32_t ax_mg, int32_t ay_mg, int32_t az_mg,
        int32_t mx, int32_t my, int32_t mz,
        uint32_t dt_ms,
        int32_t sun_heading_cdeg = 0,
        bool sun_valid = false
    ) {
        nc_imu9_t imu;
        imu.gx_bam_s = nc_cdeg_s_to_bam_s(gx_cdeg_s);
        imu.gy_bam_s = nc_cdeg_s_to_bam_s(gy_cdeg_s);
        imu.gz_bam_s = nc_cdeg_s_to_bam_s(gz_cdeg_s);
        imu.ax_mg = ax_mg;
        imu.ay_mg = ay_mg;
        imu.az_mg = az_mg;
        imu.mx = mx;
        imu.my = my;
        imu.mz = mz;
        nc_bam_t h = nc_compass_update_9dof(
            &comp_,
            &tilt_,
            &imu,
            dt_ms,
            1u,
            NC_Q15_ONE,
            nc_cdeg_to_bam(sun_heading_cdeg),
            sun_valid ? 1u : 0u,
            sun_valid ? NC_Q15_ONE : 0
        );
        return nc_bam_to_cdeg(h);
    }

    float update9DOF(
        float gx_deg_s, float gy_deg_s, float gz_deg_s,
        float ax_g, float ay_g, float az_g,
        float mx, float my, float mz,
        uint32_t dt_ms
    ) {
        int32_t cdeg = update9DOF_cdeg(
            static_cast<int32_t>(gx_deg_s * 100.0f),
            static_cast<int32_t>(gy_deg_s * 100.0f),
            static_cast<int32_t>(gz_deg_s * 100.0f),
            static_cast<int32_t>(ax_g * 1000.0f),
            static_cast<int32_t>(ay_g * 1000.0f),
            static_cast<int32_t>(az_g * 1000.0f),
            static_cast<int32_t>(mx * 100.0f),
            static_cast<int32_t>(my * 100.0f),
            static_cast<int32_t>(mz * 100.0f),
            dt_ms
        );
        return 0.01f * static_cast<float>(cdeg);
    }

    nc_bam_t headingBam() const { return nc_compass_heading_bam(&comp_); }
    int32_t headingCentiDeg() const { return nc_bam_to_cdeg(nc_compass_heading_bam(&comp_)); }
    float headingDeg() const { return 0.01f * static_cast<float>(headingCentiDeg()); }

    int32_t rollCentiDeg() const { return nc_bam_to_cdeg(nc_tilt_roll_bam(&tilt_)); }
    int32_t pitchCentiDeg() const { return nc_bam_to_cdeg(nc_tilt_pitch_bam(&tilt_)); }
    float rollDeg() const { return 0.01f * static_cast<float>(rollCentiDeg()); }
    float pitchDeg() const { return 0.01f * static_cast<float>(pitchCentiDeg()); }
    bool accelGated() const { return tilt_.accel_gated != 0u; }

    int32_t gyroBiasCentiDegS() const { return nc_bam_s_to_cdeg_s(comp_.pen_bias_bam_s); }
    float gyroBiasDegS() const { return 0.01f * static_cast<float>(gyroBiasCentiDegS()); }

    nc_compass_t& raw() { return comp_; }
    const nc_compass_t& raw() const { return comp_; }
    nc_tilt_t& rawTilt() { return tilt_; }
    const nc_tilt_t& rawTilt() const { return tilt_; }

private:
    nc_compass_t comp_;
    nc_tilt_t tilt_;
};

class NeckCompassPlasticRing {
public:
    NeckCompassPlasticRing() {
        nc_plastic_ring_init(&plastic_);
        nc_tilt_init(&tilt_);
    }

    void begin(uint16_t tilt_tau_ms = 500u) {
        nc_plastic_ring_init(&plastic_);
        nc_tilt_init(&tilt_);
        tilt_.tau_ms = tilt_tau_ms;
    }

    void setAnchorScaleQ15(int16_t anchor_scale_q15) {
        plastic_.anchor_scale_q15 = anchor_scale_q15;
    }

    int32_t update2D_cdeg(
        int32_t omega_cdeg_s,
        uint32_t dt_ms,
        int32_t raw_mag_cdeg,
        bool learn = true,
        int32_t ref_heading_cdeg = 0,
        bool ref_valid = false
    ) {
        nc_bam_t h = nc_plastic_ring_update(
            &plastic_,
            nc_cdeg_s_to_bam_s(omega_cdeg_s),
            dt_ms,
            nc_cdeg_to_bam(raw_mag_cdeg),
            nc_cdeg_to_bam(ref_heading_cdeg),
            ref_valid ? 1u : 0u,
            learn ? 1u : 0u
        );
        return nc_bam_to_cdeg(h);
    }

    int32_t update9DOF_cdeg(
        int32_t gx_cdeg_s, int32_t gy_cdeg_s, int32_t gz_cdeg_s,
        int32_t ax_mg, int32_t ay_mg, int32_t az_mg,
        int32_t mx, int32_t my, int32_t mz,
        uint32_t dt_ms,
        bool learn = true,
        int32_t ref_heading_cdeg = 0,
        bool ref_valid = false
    ) {
        nc_imu9_t imu;
        imu.gx_bam_s = nc_cdeg_s_to_bam_s(gx_cdeg_s);
        imu.gy_bam_s = nc_cdeg_s_to_bam_s(gy_cdeg_s);
        imu.gz_bam_s = nc_cdeg_s_to_bam_s(gz_cdeg_s);
        imu.ax_mg = ax_mg;
        imu.ay_mg = ay_mg;
        imu.az_mg = az_mg;
        imu.mx = mx;
        imu.my = my;
        imu.mz = mz;
        nc_bam_t h = nc_plastic_ring_update_9dof(
            &plastic_,
            &tilt_,
            &imu,
            dt_ms,
            nc_cdeg_to_bam(ref_heading_cdeg),
            ref_valid ? 1u : 0u,
            learn ? 1u : 0u
        );
        return nc_bam_to_cdeg(h);
    }

    float update9DOF(
        float gx_deg_s, float gy_deg_s, float gz_deg_s,
        float ax_g, float ay_g, float az_g,
        float mx, float my, float mz,
        uint32_t dt_ms,
        bool learn = true
    ) {
        int32_t cdeg = update9DOF_cdeg(
            static_cast<int32_t>(gx_deg_s * 100.0f),
            static_cast<int32_t>(gy_deg_s * 100.0f),
            static_cast<int32_t>(gz_deg_s * 100.0f),
            static_cast<int32_t>(ax_mg_from_g(ax_g)),
            static_cast<int32_t>(ax_mg_from_g(ay_g)),
            static_cast<int32_t>(ax_mg_from_g(az_g)),
            static_cast<int32_t>(mx * 100.0f),
            static_cast<int32_t>(my * 100.0f),
            static_cast<int32_t>(mz * 100.0f),
            dt_ms,
            learn
        );
        return 0.01f * static_cast<float>(cdeg);
    }

    int32_t mapRawMagCentiDeg(int32_t raw_mag_cdeg, int16_t* out_conf_q15 = nullptr) const {
        nc_bam_t corr_bam = 0;
        nc_plastic_ring_map(&plastic_, nc_cdeg_to_bam(raw_mag_cdeg), &corr_bam, out_conf_q15);
        return nc_bam_to_cdeg(corr_bam);
    }

    nc_bam_t headingBam() const { return nc_plastic_ring_heading_bam(&plastic_); }
    int32_t headingCentiDeg() const { return nc_bam_to_cdeg(headingBam()); }
    float headingDeg() const { return 0.01f * static_cast<float>(headingCentiDeg()); }
    int32_t rollCentiDeg() const { return nc_bam_to_cdeg(nc_tilt_roll_bam(&tilt_)); }
    int32_t pitchCentiDeg() const { return nc_bam_to_cdeg(nc_tilt_pitch_bam(&tilt_)); }
    float rollDeg() const { return 0.01f * static_cast<float>(rollCentiDeg()); }
    float pitchDeg() const { return 0.01f * static_cast<float>(pitchCentiDeg()); }
    bool accelGated() const { return tilt_.accel_gated != 0u; }

    nc_plastic_ring_t& raw() { return plastic_; }
    const nc_plastic_ring_t& raw() const { return plastic_; }
    nc_tilt_t& rawTilt() { return tilt_; }
    const nc_tilt_t& rawTilt() const { return tilt_; }

private:
    static int32_t ax_mg_from_g(float g_val) {
        return static_cast<int32_t>(g_val * 1000.0f);
    }

    nc_plastic_ring_t plastic_;
    nc_tilt_t tilt_;
};

class NeckCompassNavigator {
public:
    explicit NeckCompassNavigator(int32_t arrival_radius_mm = 1500) {
        nc_navigator_init(&nav_, arrival_radius_mm);
    }

    void begin(int32_t arrival_radius_mm = 1500) {
        nc_navigator_init(&nav_, arrival_radius_mm);
    }

    void resetOrigin() {
        nc_navigator_reset(&nav_);
    }

    void useBancConnectome() {
        nc_navigator_set_weights(&nav_, nc_banc_default_weights());
    }

    void useAnalyticV2() {
        nc_navigator_set_weights(&nav_, nullptr);
    }

    int32_t steeringCmdWithCompassCentiDegS(const NeckCompassRing& ring, int32_t max_turn_rate_cdeg_s) {
        int32_t cmd_bam_s = nc_navigator_steering_cmd_with_compass(
            &nav_,
            &ring.raw(),
            ring.headingBam(),
            nc_cdeg_s_to_bam_s(max_turn_rate_cdeg_s)
        );
        return nc_bam_s_to_cdeg_s(cmd_bam_s);
    }

    void update(int32_t speed_mm_s, uint32_t dt_ms, nc_bam_t heading_bam) {
        nc_navigator_update(&nav_, speed_mm_s, dt_ms, heading_bam);
    }

    void updateCentiDeg(int32_t speed_mm_s, uint32_t dt_ms, int32_t heading_cdeg) {
        nc_navigator_update(&nav_, speed_mm_s, dt_ms, nc_cdeg_to_bam(heading_cdeg));
    }

    uint32_t homeDistanceMm() const {
        uint32_t dist_mm = 0u;
        nc_bam_t bearing_bam = 0;
        nc_navigator_home_vector(&nav_, &dist_mm, &bearing_bam);
        return dist_mm;
    }

    int32_t homeBearingCentiDeg() const {
        uint32_t dist_mm = 0u;
        nc_bam_t bearing_bam = 0;
        nc_navigator_home_vector(&nav_, &dist_mm, &bearing_bam);
        return nc_bam_to_cdeg(bearing_bam);
    }

    float homeDistanceMeters() const {
        return 0.001f * static_cast<float>(homeDistanceMm());
    }

    float homeBearingDeg() const {
        return 0.01f * static_cast<float>(homeBearingCentiDeg());
    }

    int32_t steeringCmdCentiDegS(nc_bam_t current_heading_bam, int32_t max_turn_rate_cdeg_s) const {
        int32_t cmd_bam_s = nc_navigator_steering_cmd(
            &nav_,
            current_heading_bam,
            nc_cdeg_s_to_bam_s(max_turn_rate_cdeg_s)
        );
        return nc_bam_s_to_cdeg_s(cmd_bam_s);
    }

    float steeringCmdDegS(float current_heading_deg, float max_turn_rate_deg_s) const {
        int32_t cdeg_s = steeringCmdCentiDegS(
            nc_cdeg_to_bam(static_cast<int32_t>(current_heading_deg * 100.0f)),
            static_cast<int32_t>(max_turn_rate_deg_s * 100.0f)
        );
        return 0.01f * static_cast<float>(cdeg_s);
    }

    nc_navigator_t& raw() { return nav_; }
    const nc_navigator_t& raw() const { return nav_; }

private:
    nc_navigator_t nav_;
};

#endif /* __cplusplus */

#endif /* NECKCOMPASS_ARDUINO_HPP */
