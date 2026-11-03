"""
Physically Grounded Engineering Telemetry & Split-AI Benchmark Workloads:
  1. Offshore Smart Buoy & USV Marine Telemetry (16 ch @ 10 Hz, Iridium SBD / UHF)
  2. City-Wide AQMS Air Quality & Toxic Gas Plume Network (12 ch @ 1 Hz, LoRaWAN)
  3. Rural Tele-Health Multi-Lead ECG/PPG/Motion Wearable (8 ch @ 100 Hz, 2G/M2M)
  4. Multi-Regime Sequential Sensor Streams for Edge-to-Cloud Split-AI (GPU)
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np


def generate_marine_buoy_usv_telemetry(n_steps: int = 3000, seed: int = 101) -> Tuple[np.ndarray, Dict[str, object]]:
    """
    16-channel 10 Hz marine buoy / USV telemetry (heave, pitch, roll, surge, sway, yaw_rate,
    port_rpm, stbd_rpm, port_amps, stbd_amps, DO, pH, turbidity, chlorophyll, wind_spd, wind_dir).
    Combines calm swells, intermittent Sea-State-3 wave groups, and sudden pod fouling / gust events.
    """
    rng = np.random.default_rng(seed)
    dt = 0.1  # 10 Hz
    t = np.arange(n_steps) * dt
    x = np.zeros((n_steps, 16), dtype=np.float64)

    # Wave envelope: mostly calm swell (0.15x) with 4 energetic wave-group bursts (1.0x)
    env = np.full(n_steps, 0.12, dtype=np.float64)
    burst_centers = rng.uniform(0.12, 0.90, size=5) * t[-1]
    for bc in burst_centers:
        env += 1.15 * np.exp(-0.5 * ((t - bc) / 9.5) ** 2)

    # 6-DOF JONSWAP-like multi-component wave motion
    freqs = np.array([0.14, 0.19, 0.26, 0.35])
    for k_dof in range(6):
        sig = np.zeros(n_steps)
        for f in freqs:
            ph = rng.uniform(0, 2 * np.pi)
            amp = rng.uniform(0.25, 0.75)
            sig += amp * np.sin(2 * np.pi * f * t + ph)
        x[:, k_dof] = env * sig + rng.normal(0.0, 0.015, size=n_steps)

    # Twin-pod RPM & motor current: steady cruise with 3 sharp maneuvering / gillnet drag events
    base_rpm = 1200.0
    x[:, 6] = base_rpm + 15.0 * env * np.sin(2 * np.pi * 0.18 * t)
    x[:, 7] = base_rpm + 15.0 * env * np.cos(2 * np.pi * 0.18 * t)
    x[:, 8] = 14.0 + 1.2 * env
    x[:, 9] = 14.0 + 1.2 * env
    for ev_t in rng.uniform(0.2, 0.85, size=3) * t[-1]:
        mask = (t >= ev_t) & (t < ev_t + 8.0)
        decay = np.exp(-(t[mask] - ev_t) / 3.0)
        x[mask, 7] -= 480.0 * decay   # Starboard pod RPM drop
        x[mask, 9] += 19.5 * decay    # Starboard current spike
        x[mask, 6] += 220.0 * decay   # Port pod compensation

    # Water quality & met-ocean slow baselines + turbidity sediment plumes & wind gusts
    x[:, 10] = 7.4 + 0.08 * np.sin(2 * np.pi * t / 180.0)   # DO mg/L
    x[:, 11] = 8.12 + 0.02 * np.cos(2 * np.pi * t / 240.0)  # pH
    x[:, 12] = 4.5 + rng.normal(0.0, 0.04, size=n_steps)    # Turbidity NTU
    x[:, 13] = 2.1 + 0.05 * np.sin(2 * np.pi * t / 200.0)   # Chl-a ug/L
    x[:, 14] = 5.8 + 0.3 * np.sin(2 * np.pi * t / 90.0)     # Wind m/s
    x[:, 15] = 215.0 + 3.0 * np.sin(2 * np.pi * t / 120.0)  # Wind deg

    for pl_t in rng.uniform(0.15, 0.9, size=4) * t[-1]:
        plume = np.exp(-0.5 * ((t - pl_t) / 4.0) ** 2)
        x[:, 12] += 28.0 * plume
        x[:, 14] += 9.5 * plume

    meta = {
        "name": "Smart Buoy & USV Marine Telemetry",
        "sample_rate_hz": 10.0,
        "duration_s": float(n_steps * dt),
        "rtt_ms": 1200.0,  # Iridium SBD satellite RTT
        "link_type": "iridium_sbd_satellite",
    }
    return x, meta


def generate_aqms_gas_plume_telemetry(n_steps: int = 3600, seed: int = 202) -> Tuple[np.ndarray, Dict[str, object]]:
    """
    12-channel 1 Hz City AQMS & Industrial Perimeter Network:
    (H2S, NH3, SO2, NO2, VOC, PM2.5, PM10, CO, O3, Temp, RH, WindSpd).
    Mostly calm diurnal drift punctuated by sudden toxic H2S / NH3 / VOC leak puffs.
    """
    rng = np.random.default_rng(seed)
    dt = 1.0  # 1 Hz
    t = np.arange(n_steps) * dt
    x = np.zeros((n_steps, 12), dtype=np.float64)

    # Slow diurnal drift across background pollutants & meteorology
    for c in range(12):
        ph = rng.uniform(0, 2 * np.pi)
        x[:, c] = 10.0 + 0.8 * np.sin(2 * np.pi * t / 1800.0 + ph) + rng.normal(0.0, 0.012, size=n_steps)

    # Inject 8 sharp industrial gas leak plumes (H2S ch0, NH3 ch1, VOC ch4, PM2.5 ch5)
    spike_mask = np.zeros(n_steps, dtype=bool)
    plume_times = np.linspace(260.0, n_steps - 240.0, 8) + rng.uniform(-40.0, 40.0, size=8)
    for idx_p, pt in enumerate(plume_times):
        width = rng.uniform(5.0, 11.0)
        puff = np.exp(-0.5 * ((t - pt) / width) ** 2)
        target_ch = [0, 1, 4, 5][idx_p % 4]
        amp = rng.uniform(14.0, 26.0)
        x[:, target_ch] += amp * puff
        x[:, (target_ch + 1) % 6] += 0.45 * amp * puff
        spike_mask |= (puff > 0.35)

    meta = {
        "name": "City AQMS & Toxic Gas Plume Network",
        "sample_rate_hz": 1.0,
        "duration_s": float(n_steps * dt),
        "rtt_ms": 420.0,  # LoRaWAN / 2G M2M RTT
        "link_type": "lorawan_sf9",
        "spike_mask": spike_mask,
    }
    return x, meta


def generate_rural_tele_ecg_wearable(n_steps: int = 4000, seed: int = 303) -> Tuple[np.ndarray, Dict[str, object]]:
    """
    8-channel 100 Hz Rural Tele-Health Wearable (3-lead ECG, PPG, Resp, 3-axis Accel).
    Sharp QRS cardiac spikes separated by flat ST/TP baselines + motion bursts.
    """
    rng = np.random.default_rng(seed)
    dt = 0.01  # 100 Hz
    t = np.arange(n_steps) * dt
    x = np.zeros((n_steps, 8), dtype=np.float64)

    hr_hz = 1.22  # ~73 BPM
    phase = np.mod(t * hr_hz, 1.0)
    # Synthetic P-Q-R-S-T complex
    p_wave = 0.14 * np.exp(-0.5 * ((phase - 0.18) / 0.025) ** 2)
    q_wave = -0.16 * np.exp(-0.5 * ((phase - 0.28) / 0.010) ** 2)
    r_wave = 1.45 * np.exp(-0.5 * ((phase - 0.305) / 0.011) ** 2)
    s_wave = -0.28 * np.exp(-0.5 * ((phase - 0.33) / 0.012) ** 2)
    t_wave = 0.28 * np.exp(-0.5 * ((phase - 0.54) / 0.040) ** 2)
    ecg_base = p_wave + q_wave + r_wave + s_wave + t_wave

    resp = np.sin(2 * np.pi * 0.24 * t)
    x[:, 0] = ecg_base * (1.0 + 0.06 * resp) + rng.normal(0.0, 0.008, size=n_steps)
    x[:, 1] = 0.72 * ecg_base - 0.15 * r_wave + rng.normal(0.0, 0.008, size=n_steps)
    x[:, 2] = -0.38 * ecg_base + 0.55 * t_wave + rng.normal(0.0, 0.008, size=n_steps)
    # PPG pulse wave
    x[:, 3] = 0.85 * np.exp(-0.5 * ((phase - 0.44) / 0.065) ** 2) + 0.24 * np.exp(-0.5 * ((phase - 0.62) / 0.055) ** 2)
    x[:, 4] = resp

    # 3-axis accelerometer: mostly resting posture with 3 sudden stumble/walking bursts
    for ax in range(5, 8):
        x[:, ax] = rng.normal(0.0, 0.01, size=n_steps)
    for bt in (8.5, 21.0, 32.5):
        m_env = np.exp(-0.5 * ((t - bt) / 1.4) ** 2)
        x[:, 5] += 1.8 * m_env * np.sin(2 * np.pi * 3.5 * t)
        x[:, 6] += 1.4 * m_env * np.cos(2 * np.pi * 3.5 * t)
        x[:, 7] += 2.2 * m_env * np.sin(2 * np.pi * 7.0 * t)

    r_peak_mask = phase > 0.285
    r_peak_mask &= (phase < 0.325)
    meta = {
        "name": "Rural Tele-Health ECG/PPG Wearable",
        "sample_rate_hz": 100.0,
        "duration_s": float(n_steps * dt),
        "rtt_ms": 95.0,  # Rural cellular RTT
        "link_type": "cellular_m2m",
        "spike_mask": r_peak_mask,
    }
    return x, meta


def generate_split_ai_sequence_dataset(
    n_sequences: int = 640,
    seq_len: int = 64,
    in_dim: int = 24,
    seed: int = 404,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Generates multi-channel sequential sensor streams (`(N_seq, T, 24)`) with 8 distinct
    physical operating/hazard classes (0: calm nominal, 1: wave slam, 2: starboard pod snag,
    3: port pod brownout, 4: toxic gas puff, 5: sensor drift, 6: high-freq vibration, 7: emergency turn)
    for training & benchmarking GPU Edge-to-Cloud Split-AI latent streaming.
    """
    rng = np.random.default_rng(seed)
    x_all = np.zeros((n_sequences, seq_len, in_dim), dtype=np.float32)
    y_all = np.zeros((n_sequences, seq_len), dtype=np.int64)
    t = np.linspace(0.0, 1.0, seq_len, dtype=np.float32)

    for s in range(n_sequences):
        # Start calm (class 0) for first ~14..22 steps, then transition into an event regime
        cls = int(s % 8)
        onset = int(rng.integers(12, 24)) if cls != 0 else seq_len
        base = rng.normal(0.0, 0.08, size=(seq_len, in_dim)).astype(np.float32)
        for ch in range(in_dim):
            base[:, ch] += 0.25 * np.sin(2.0 * np.pi * (1.0 + 0.15 * (ch % 5)) * t + rng.uniform(0, 6.28))

        if cls > 0:
            y_all[s, onset:] = cls
            dur = seq_len - onset
            tt = np.linspace(0.0, 1.0, dur, dtype=np.float32)
            env = np.minimum(1.0, tt * 4.0)
            if cls == 1:  # Wave slam: synchronous oscillatory surge on ch 0..5
                for ch in range(6):
                    base[onset:, ch] += env * 2.1 * np.sin(2 * np.pi * 4.0 * tt + 0.4 * ch)
            elif cls == 2:  # Starboard pod snag: ch 6 drops, ch 7 spikes
                base[onset:, 6] -= env * 2.4
                base[onset:, 7] += env * 2.3
                base[onset:, 8] += env * 1.1 * np.sin(2 * np.pi * 3.0 * tt)
            elif cls == 3:  # Port pod brownout: ch 9, 10 drop symmetrically
                base[onset:, 9] -= env * 2.2
                base[onset:, 10] -= env * 2.0
            elif cls == 4:  # Toxic gas puff: sharp exponential rise on ch 12..14
                puff = env * np.exp(-1.2 * tt) * 3.1
                base[onset:, 12] += puff
                base[onset:, 13] += 0.8 * puff
                base[onset:, 14] -= 0.6 * puff
            elif cls == 5:  # Calibration/membrane drift: ramp on ch 15..17
                base[onset:, 15] += tt * 2.8
                base[onset:, 16] -= tt * 2.4
                base[onset:, 17] += tt * 1.9
            elif cls == 6:  # Bearing high-freq vibration: ch 18..20 alternating sign
                vib = env * 2.0 * np.cos(2 * np.pi * 9.0 * tt)
                base[onset:, 18] += vib
                base[onset:, 19] -= vib
                base[onset:, 20] += 0.7 * vib
            elif cls == 7:  # Coordinated emergency turn: opposing ramps ch 21..23 & ch 0..2
                base[onset:, 21] += env * 2.5
                base[onset:, 22] -= env * 2.5
                base[onset:, 23] += env * 1.8

        x_all[s] = base

    return x_all, y_all
