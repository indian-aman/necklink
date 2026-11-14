/*
 * ESP32_Multicopter_USV_FaultRecovery.ino
 *
 * Demonstrates NeckCord v2.0.0 pure-integer (< 1 KB RAM) real-time control:
 *   1. 4-byte Descending Cervical Command Frame decode + CRC-4 verification
 *   2. Sensorless ESC (RPM + Current) broken-blade detection in < 50 ms +
 *      4-DOF [0, u_max] 6-rotor hexacopter motor-loss hover recovery
 *   3. Asymmetric BlueRobotics T200 (-40.0 N / +52.5 N) USV fault allocation
 *   4. 6-leg Kuramoto VNC CPG + exact 18-servo 3-DOF leg IK with +45 mm parking
 *
 * Compiles out-of-the-box on ESP32, RP2040, STM32, Teensy, and AVR boards.
 */

#include <Arduino.h>
#include <NeckCord.h>

static nc_shunting4d_t      g_hex6;
static nc_alloc4d_result_t  g_hex_res;
static nc_esc_observer_t    g_esc_obs;
static nc_esc_obs_result_t  g_esc_res;

static nc_shunting4d_t      g_usv_t200;
static nc_alloc4d_result_t  g_usv_res;

static nc_cpg_cord_t        g_cpg;
static nc_cpg_state_t       g_cpg_state;
static nc_hexapod_ik_cfg_t  g_ik_cfg;
static nc_hexapod_ik_out_t  g_ik_out;

static uint32_t g_tick = 0;

/* Convert Q15 [0..32767] rotor thrust command into standard 1000..2000 us ESC PWM */
static uint16_t rotor_q15_to_pwm_us(int16_t cmd_q15)
{
    if (cmd_q15 <= 0) {
        return 1000u;
    }
    return (uint16_t)(1000u + (((uint32_t)cmd_q15 * 1000u + 16383u) / 32767u));
}

void setup(void)
{
    uint8_t golden_wire[4] = { 0x68u, 0xE0u, 0x0Cu, 0x20u };
    nc_descending_frame_t rx_cmd;

    Serial.begin(115200);
    delay(200);
    Serial.println(F("=== NeckCord v2.0.0 Embedded Fault-Recovery Demo ==="));

    /* 1. Verify 4-byte descending cervical wire frame */
    if (nc_decode_descending_4b(golden_wire, 2, &rx_cmd) == NC_OK) {
        Serial.print(F("[Cervical 4B] Decoded OK | fwd_q15="));
        Serial.print(rx_cmd.drive_fwd_q15);
        Serial.print(F(" yaw_q15="));
        Serial.println(rx_cmd.turn_yaw_q15);
    }

    /* 2. Initialize 6-rotor [0, u_max] hexacopter (280 mm arm, k_yaw=0.018) + ESC observer */
    nc_shunting4d_init_hex6(&g_hex6, 280, 18);
    g_hex6.tau_act_ms = 0u;
    nc_esc_observer_init(&g_esc_obs, 6u, 11500, 450, 21500);
    g_esc_obs.tau_motor_ms = 0u;

    /* 3. Initialize asymmetric BlueRobotics T200 4-thruster USV (-40 N / +52.5 N, 6% deadzone) */
    nc_shunting4d_init_usv_t200(&g_usv_t200, 450, 350, -24965, 1966);
    nc_shunting4d_set_health(&g_usv_t200, 2u, 0); /* Port-Aft thruster fouled */

    /* 4. Initialize 6-leg CPG + 18-servo 3-DOF IK with L2 jammed */
    nc_cpg_init(&g_cpg, 1u);
    nc_cpg_set_limb_health(&g_cpg, 2u, 0);
    nc_hexapod_ik_default_cfg(&g_ik_cfg);
}

void loop(void)
{
    /* Hover Fz = 2.0 * u_max (32768 Q14), nose-up pitch Ty = -1092 Q14 */
    int32_t w_hover_q14[4] = { 32768, 0, -1092, 0 };
    int32_t w_usv_rev_q14[3] = { -22000, 0, 3200 };
    int32_t rpm_meas[6];
    int32_t current_ma[6];
    nc_descending_frame_t walk_cmd;
    uint8_t i;

    if (g_tick == 0u) {
        nc_shunting4d_step(&g_hex6, w_hover_q14, NULL, 5u, &g_hex_res);
    }

    /* Simulate Rotor 0 shearing a propeller blade at tick 10 (t = 50 ms):
     * motor spins freely at normal RPM, while load current collapses. */
    for (i = 0u; i < 6u; ++i) {
        uint32_t su = nc_isqrt_u64((uint64_t)(uint32_t)g_hex_res.cmd_q15[i] * 32767ULL);
        int32_t r_nom = (int32_t)(((int64_t)11500 * (int64_t)su) / 32767);
        int32_t nr_q15 = (int32_t)(((int64_t)r_nom * 32767) / 11500);
        int32_t r3_q15 = (int32_t)((((int64_t)nr_q15 * nr_q15) / 32767 * nr_q15) / 32767);
        int32_t load_ma = (int32_t)(((int64_t)21500 * r3_q15) / 32767);
        rpm_meas[i] = r_nom;
        current_ma[i] = 450 + ((g_tick >= 10u && i == 0u) ? (load_ma / 15) : load_ma);
    }

    nc_esc_observer_update(&g_esc_obs, g_hex_res.cmd_q15, rpm_meas, current_ma, 5u, &g_esc_res);
    nc_shunting4d_step(&g_hex6, w_hover_q14, g_esc_res.est_thrust_q15, 5u, &g_hex_res);
    nc_shunting4d_allocate(&g_usv_t200, w_usv_rev_q14, &g_usv_res);

    memset(&walk_cmd, 0, sizeof(walk_cmd));
    walk_cmd.drive_fwd_q15 = 26000;
    nc_cpg_step(&g_cpg, &walk_cmd, 5u, &g_cpg_state);
    nc_hexapod_ik_3dof(&g_ik_cfg, &g_cpg, &g_cpg_state, &g_ik_out);

    if ((g_tick % 40u) == 0u) {
        Serial.print(F("t="));
        Serial.print(g_tick * 5u);
        Serial.print(F("ms | Hex6 broken_mask=0x"));
        Serial.print(g_esc_res.broken_prop_mask, HEX);
        Serial.print(F(" R0_pwm="));
        Serial.print(rotor_q15_to_pwm_us(g_hex_res.cmd_q15[0]));
        Serial.print(F("us R1_pwm="));
        Serial.print(rotor_q15_to_pwm_us(g_hex_res.cmd_q15[1]));
        Serial.print(F("us | T200_u0="));
        Serial.print(g_usv_res.cmd_q15[0]);
        Serial.print(F(" | Pentapod L2_z="));
        Serial.print(g_ik_out.foot_z_mm[2]);
        Serial.println(F("mm"));
    }

    ++g_tick;
    delay(5);
}
