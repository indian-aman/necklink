"""Comprehensive test suite for `neckcord` v3.0.0 (including 100% v1.x/v2.x backward compatibility)."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import numpy as np
import pytest

import neckcord
from neckcord import (
    CervicalBottleneckLink,
    CervicalCRCError,
    DescendingFrame,
    ESCFaultCode,
    ESCObserver,
    GaitMode,
    HexapodLegIK,
    LIMB_NAMES,
    SegmentalCPGCord,
    ShuntingThrusterCord,
    compute_tripod_order,
    decode_descending_4b,
    encode_descending_4b,
    load_banc_vnc_weights,
    maslov_sneppen_shuffle_vnc,
)
from neckcord.cli import main as cli_main


def _circ_dist(a: float, b: float) -> float:
    d = abs((a - b + math.pi) % (2.0 * math.pi) - math.pi)
    return d


def test_cervical_4b_roundtrip_sweep() -> None:
    """4-byte cervical codec preserves zeros, full-scale endpoints, gait, freeze, and seq."""
    zero_raw = encode_descending_4b(DescendingFrame())
    assert len(zero_raw) == 4
    z_dec = decode_descending_4b(zero_raw)
    assert z_dec.drive_fwd == 0.0
    assert z_dec.turn_yaw == 0.0
    assert z_dec.strafe_lat == 0.0
    assert z_dec.gait_mode == 0
    assert z_dec.freeze_reflex is False

    # Golden cross-language wire vector (must equal 0x68E00C20 in Python and ANSI C99)
    golden = DescendingFrame(
        drive_fwd=0.82,
        turn_yaw=-0.25,
        strafe_lat=0.10,
        gait_mode=int(GaitMode.AUTO),
        freeze_reflex=False,
        seq=2,
    )
    assert golden.to_bytes().hex() == "68e00c20"

    for gait in range(4):
        for freeze in (False, True):
            for seq in range(4):
                tx = DescendingFrame(
                    drive_fwd=-1.0 + 0.65 * gait,
                    turn_yaw=1.0 - 0.55 * seq,
                    strafe_lat=-0.75 + 0.45 * gait,
                    gait_mode=gait,
                    freeze_reflex=freeze,
                    seq=seq,
                )
                raw = encode_descending_4b(tx)
                assert len(raw) == 4
                rx = decode_descending_4b(raw, expected_seq=seq)
                assert abs(rx.drive_fwd - tx.drive_fwd) <= (0.5 / 127.0 + 1e-6)
                assert abs(rx.turn_yaw - tx.turn_yaw) <= (0.5 / 127.0 + 1e-6)
                assert abs(rx.strafe_lat - tx.strafe_lat) <= (0.5 / 63.0 + 1e-6)
                assert rx.gait_mode == gait
                assert rx.freeze_reflex is freeze
                assert rx.seq == seq


def test_cervical_crc4_bitflip_and_stuck_bus_rejection() -> None:
    """CRC-4 rejects stuck-0, stuck-1, invalid lengths, sequence mismatch, and 100% of 1-bit flips."""
    with pytest.raises(CervicalCRCError):
        decode_descending_4b(b"\x00\x00\x00\x00")
    with pytest.raises(CervicalCRCError):
        decode_descending_4b(b"\xff\xff\xff\xff")
    with pytest.raises(CervicalCRCError):
        decode_descending_4b(b"\x68\xe0\x0c")
    with pytest.raises(CervicalCRCError):
        decode_descending_4b(b"\x68\xe0\x0c\x20\x00")

    valid = encode_descending_4b(drive_fwd=0.5, seq=1)
    with pytest.raises(CervicalCRCError):
        decode_descending_4b(valid, expected_seq=2)

    rng = np.random.default_rng(42)
    for _ in range(200):
        raw = bytearray(
            encode_descending_4b(
                drive_fwd=float(rng.uniform(-1.0, 1.0)),
                turn_yaw=float(rng.uniform(-1.0, 1.0)),
                strafe_lat=float(rng.uniform(-1.0, 1.0)),
                gait_mode=int(rng.integers(0, 4)),
                freeze_reflex=bool(rng.integers(0, 2)),
                seq=int(rng.integers(0, 4)),
            )
        )
        for bit in range(32):
            corrupted = bytearray(raw)
            corrupted[bit // 8] ^= 1 << (bit % 8)
            with pytest.raises(CervicalCRCError):
                decode_descending_4b(corrupted)


def test_cervical_bottleneck_link_rolling_seq_and_bandwidth() -> None:
    """Stateful link tracks rolling sequence gaps and confirms 400 B/s @ 100 Hz."""
    link = CervicalBottleneckLink()
    assert link.wire_bandwidth_bps(100.0) == 400.0

    p0 = link.pack(DescendingFrame(drive_fwd=0.4))
    p1 = link.pack(DescendingFrame(drive_fwd=0.5))  # will simulate dropping p1
    p2 = link.pack(DescendingFrame(drive_fwd=0.6))
    assert p1 != p2

    f0 = link.unpack(p0)
    f2 = link.unpack(p2)
    assert f0.seq == 0
    assert f2.seq == 2
    assert link.dropped_frames == 1
    assert link.rx_frames == 2


def test_cpg_tripod_antiphase_convergence() -> None:
    """Perturbed oscillator phases converge to emergent BANC Tripod (>0.90) & v2 analytic lock."""
    rng = np.random.default_rng(7)
    cpg_banc = SegmentalCPGCord()
    cpg_banc.phases = rng.uniform(0.0, 2.0 * math.pi, size=6)

    for _ in range(250):
        st_banc = cpg_banc.step(0.90, dt=0.01)

    assert st_banc.active_gait == "tripod"
    assert st_banc.gait_provenance == "connectome"
    assert st_banc.tripod_order > 0.90
    assert abs(_circ_dist(st_banc.phases[2], st_banc.phases[3]) - math.pi) < 0.15

    rng_v2 = np.random.default_rng(7)
    cpg_v2 = SegmentalCPGCord(connectome_weights="v2_analytic")
    cpg_v2.phases = rng_v2.uniform(0.0, 2.0 * math.pi, size=6)
    for _ in range(250):
        st_v2 = cpg_v2.step(0.90, dt=0.01)
    assert st_v2.active_gait == "tripod"
    assert _circ_dist(st_v2.phases[0], st_v2.phases[3]) < 0.08
    assert _circ_dist(st_v2.phases[0], st_v2.phases[4]) < 0.08
    assert abs(_circ_dist(st_v2.phases[0], st_v2.phases[1]) - math.pi) < 0.08
    assert abs(_circ_dist(st_v2.phases[2], st_v2.phases[3]) - math.pi) < 0.08


def test_cpg_gait_transitions_wave_tetrapod_tripod() -> None:
    """Single scalar descending `drive_fwd` smoothly transitions Wave -> Tetrapod -> Tripod."""
    cpg = SegmentalCPGCord()

    for _ in range(150):
        st_wave = cpg.step(0.20, dt=0.01)
    assert st_wave.active_gait == "wave"
    assert st_wave.duty_factor == pytest.approx(0.82, abs=0.02)

    max_step_jump = 0.0
    prev_phases = st_wave.phases.copy()
    for k in range(200):
        drive = 0.20 + 0.65 * (k / 199.0)
        st_mid = cpg.step(drive, dt=0.01)
        if k == 95:
            assert st_mid.active_gait == "tetrapod"
        step_jump = max(_circ_dist(float(st_mid.phases[i]), float(prev_phases[i])) for i in range(6))
        max_step_jump = max(max_step_jump, step_jump)
        prev_phases = st_mid.phases.copy()

    assert st_mid.active_gait == "tripod"
    assert st_mid.duty_factor == pytest.approx(0.54, abs=0.02)
    assert max_step_jump < 0.75  # Smooth continuous ODE phase evolution (<< pi/3 jump)


def test_cpg_mdn_reverse_and_dna02_steering() -> None:
    """Negative `drive_fwd` (MDN) reverses phase progression; `turn_yaw` (DNa02) biases sides."""
    cpg = SegmentalCPGCord()
    p_start = cpg.phases[0]
    cpg.step(-0.75, dt=0.01)
    # Wrapped backward step
    delta = (cpg.phases[0] - p_start + math.pi) % (2.0 * math.pi) - math.pi
    assert delta < 0.0

    # Positive CCW turn_yaw -> right legs sweep larger longitudinal excursion than left legs
    cpg_turn = SegmentalCPGCord()
    st = cpg_turn.step(DescendingFrame(drive_fwd=0.6, turn_yaw=0.5, gait_mode=int(GaitMode.TRIPOD)), dt=0.01)
    # L1 (index 0) and R2 (index 3) share identical phase 0 in Tripod, so compare their x offsets from anchor
    dx_left = abs(st.foot_positions_xy[0, 0] - 0.20)
    dx_right = abs(st.foot_positions_xy[3, 0] - 0.00)
    assert dx_right > dx_left


def test_cpg_giant_fiber_freeze_reflex() -> None:
    """`freeze_reflex=True` immediately halts phase drift and zeroes descending wrench."""
    cpg = SegmentalCPGCord()
    cpg.step(0.8, dt=0.02)
    frozen_frame = DescendingFrame(drive_fwd=0.9, turn_yaw=-0.4, freeze_reflex=True)
    assert np.allclose(frozen_frame.to_wrench(), np.zeros(3))

    st1 = cpg.step(frozen_frame, dt=0.02)
    st2 = cpg.step(frozen_frame, dt=0.02)
    assert st2.active_gait == "freeze"
    assert np.allclose(st1.phases, st2.phases)
    assert np.all(st2.stance_mask)
    assert st2.is_statically_stable is True


def test_cpg_single_leg_loss_L2_static_stability() -> None:
    """Jamming L2 drops unadapted Tripod stability <60%, while adaptive VNC achieves >=98%."""
    cmd = DescendingFrame(drive_fwd=0.85, gait_mode=int(GaitMode.AUTO))

    cpg_adapt = SegmentalCPGCord(adapt_to_damage=True)
    cpg_fixed = SegmentalCPGCord(adapt_to_damage=False)
    cpg_adapt.inject_limb_fault("L2")
    cpg_fixed.inject_limb_fault("L2")

    for _ in range(100):
        cpg_adapt.step(cmd, dt=0.01)
        cpg_fixed.step(cmd, dt=0.01)

    adapt_stable = 0
    fixed_stable = 0
    total = 400
    for _ in range(total):
        sa = cpg_adapt.step(cmd, dt=0.01)
        sf = cpg_fixed.step(cmd, dt=0.01)
        assert not sa.stance_mask[2]
        if sa.is_statically_stable:
            adapt_stable += 1
        if sf.is_statically_stable:
            fixed_stable += 1

    assert adapt_stable / total >= 0.98
    assert fixed_stable / total < 0.60


@pytest.mark.parametrize("limb_name", LIMB_NAMES)
def test_cpg_all_six_single_leg_amputations_stable(limb_name: str) -> None:
    """Every single-leg loss (L1, R1, L2, R2, L3, R3) self-heals to >=95% polygon stability."""
    cpg = SegmentalCPGCord(adapt_to_damage=True)
    cpg.inject_limb_fault(limb_name)
    cmd = DescendingFrame(drive_fwd=0.80)

    for _ in range(100):
        cpg.step(cmd, dt=0.01)

    stable = sum(1 for _ in range(300) if cpg.step(cmd, dt=0.01).is_statically_stable)
    assert stable / 300.0 >= 0.95


def test_cpg_online_campaniform_load_fault_detection() -> None:
    """Zero substrate load during expected stance triggers automatic pentapod shift."""
    cpg = SegmentalCPGCord(adapt_to_damage=True)
    cmd = DescendingFrame(drive_fwd=0.80)
    st = cpg.step(cmd, dt=0.01)

    for _ in range(60):
        loads = np.ones(6, dtype=np.float64)
        loads[3] = 0.0  # R2 loses ground contact / jams in air
        cpg.update_proprioceptive_load(loads, st.stance_mask, dt=0.01, tau_fault=0.04)
        st = cpg.step(cmd, dt=0.01)

    assert cpg.limb_health[3] < cpg.fault_threshold
    assert st.active_gait == "adaptive_pentapod"


def test_allocator_nominal_usv4_exact_wrench() -> None:
    """Intact 4-thruster vectored USV allocator matches Moore-Penrose pseudo-inverse."""
    cord = ShuntingThrusterCord.usv_vectored_4(lx=0.45, ly=0.35, cant_deg=45.0, u_max=35.0)
    w_pinv = np.linalg.pinv(cord.W)

    for w_des in (
        np.array([30.0, 0.0, 0.0]),
        np.array([20.0, -8.0, 5.0]),
        np.array([-15.0, 6.0, -4.0]),
    ):
        res = cord.allocate(w_des)
        u_ref = w_pinv @ w_des
        assert np.allclose(res.commands, u_ref, atol=1e-2)
        assert np.allclose(res.predicted_wrench, w_des, atol=1e-2)


def test_allocator_usv4_online_fault_isolation_and_zero_spin() -> None:
    """90% loss on Port-Aft thruster T2 is isolated in <=80 ms with zero yaw spin."""
    cord = ShuntingThrusterCord.usv_vectored_4(u_max=35.0, tau_actuator=0.0)
    w_des = np.array([35.0, 0.0, 0.0])
    true_eff = np.array([1.0, 1.0, 0.10, 1.0])

    res = cord.step(w_des, dt=0.02)
    isolated_ms: float | None = None
    for tick in range(1, 8):
        fb = true_eff * res.commands
        res = cord.step(w_des, feedback_thrust=fb, dt=0.02)
        if isolated_ms is None and bool(res.isolated_mask[2]):
            isolated_ms = tick * 20.0

    assert isolated_ms is not None and isolated_ms <= 80.0
    assert res.commands[2] == 0.0
    delivered = cord.W @ (true_eff * res.commands)
    assert delivered[0] == pytest.approx(35.0, abs=0.08)
    assert abs(delivered[1]) < 0.05  # Zero uncommanded sway
    assert abs(delivered[2]) < 0.05  # Zero uncommanded yaw spin


def test_allocator_imu_wrench_residual_pathway() -> None:
    """Hull-level IMU `measured_wrench` detects thruster loss even without ESC RPM sensors."""
    cord = ShuntingThrusterCord.usv_vectored_4(u_max=35.0, tau_actuator=0.0, tau_fault=0.02)
    true_eff = np.array([1.0, 0.0, 1.0, 1.0])  # T1 dead

    rng = np.random.default_rng(11)
    res = cord.step(np.array([25.0, 4.0, -3.0]), dt=0.02)
    for _ in range(35):
        w_cmd = np.array(
            [
                float(rng.uniform(18.0, 32.0)),
                float(rng.uniform(-6.0, 6.0)),
                float(rng.uniform(-4.0, 4.0)),
            ]
        )
        w_meas = cord.W @ (true_eff * res.commands)
        res = cord.step(w_cmd, measured_wrench=w_meas, dt=0.02)

    assert bool(res.isolated_mask[1]) is True
    w_test = np.array([28.0, 0.0, 0.0])
    res_final = cord.allocate(w_test)
    delivered = cord.W @ (true_eff * res_final.commands)
    assert np.allclose(delivered, w_test, atol=0.25)


def test_allocator_yaw_sway_priority_under_saturation() -> None:
    """When surge saturates 3 surviving thrusters, yaw torque authority is preserved."""
    cord = ShuntingThrusterCord.usv_vectored_4(u_max=35.0, prioritize_yaw_sway=True)
    cord.set_actuator_health(2, 0.0)  # Port-Aft dead

    w_extreme = np.array([120.0, 0.0, 8.0])  # Exceeds max surge (~49.5 N), plus yaw demand
    res = cord.allocate(w_extreme)
    delivered = cord.W @ (np.array([1.0, 1.0, 0.0, 1.0]) * res.commands)

    assert np.max(np.abs(res.commands)) <= 35.0 + 1e-9
    assert delivered[2] == pytest.approx(8.0, abs=0.1)  # Yaw preserved!
    assert delivered[0] > 20.0  # Remaining capacity cleanly allocated to surge


def test_allocator_6_and_8_actuator_multi_fault() -> None:
    """6- and 8-thruster ROV allocators survive 2 simultaneous dead thrusters accurately."""
    cord6 = ShuntingThrusterCord.hex_vectored_6(u_max=30.0)
    cord6.set_actuator_health(0, 0.0)
    cord6.set_actuator_health(3, 0.0)
    w6 = np.array([22.0, -5.0, 3.5])
    r6 = cord6.allocate(w6)
    assert r6.commands[0] == 0.0 and r6.commands[3] == 0.0
    assert np.allclose(r6.predicted_wrench, w6, atol=0.05)

    cord8 = ShuntingThrusterCord.octo_vectored_8(u_max=25.0)
    cord8.set_actuator_health(2, 0.0)
    cord8.set_actuator_health(5, 0.0)
    w8 = np.array([30.0, 7.0, -4.0])
    r8 = cord8.allocate(w8)
    assert r8.commands[2] == 0.0 and r8.commands[5] == 0.0
    assert np.allclose(r8.predicted_wrench, w8, atol=0.05)


def test_cli_demo_and_benchmark_quick_json(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI `demo --json` and `benchmark-quick --json` execute cleanly and return valid metrics."""
    rc_demo = cli_main(["demo", "--json"])
    assert rc_demo == 0
    demo_out = json.loads(capsys.readouterr().out)
    assert demo_out["wire_bytes"] == 4
    assert demo_out["bandwidth_100hz_bps"] == 400
    assert demo_out["damaged_L2_stability_pct"] >= 98.0

    rc_bench = cli_main(["benchmark-quick", "--seeds", "2", "--json"])
    assert rc_bench == 0
    bench_out = json.loads(capsys.readouterr().out)
    u = bench_out["usv_fault_90pct_T2"]
    assert u["neckcord_peak_yaw_deg"] < u["static_pinv_peak_yaw_deg"] * 0.4
    assert 0.0 < u["neckcord_isolation_ms"] <= 100.0


def test_c99_zero_float_zero_malloc_source_audit() -> None:
    """Verify embedded ANSI C99 core contains zero floating-point or heap tokens."""
    csrc_dir = Path(__file__).resolve().parents[1] / "csrc"
    forbidden = re.compile(r"\b(float|double|malloc|calloc|realloc|free)\b")
    for fname in ("neckcord.h", "neckcord.c"):
        text = (csrc_dir / fname).read_text(encoding="utf-8")
        matches = forbidden.findall(text)
        assert matches == [], f"Forbidden token(s) {matches} found in csrc/{fname}"



def _drive_usv4(cord, alloc, eff_t2: float, ticks: int):
    eff = np.array([1.0, 1.0, eff_t2, 1.0])
    for _ in range(ticks):
        alloc = cord.step(np.array([35.0, 0.0, 0.0]), feedback_thrust=eff * alloc.commands, dt=0.02)
    return alloc


def test_recovery_probe_returns_cleared_thruster_to_service() -> None:
    cord = ShuntingThrusterCord.usv_vectored_4(tau_actuator=0.0, probe_interval_s=2.0)
    alloc = cord.step(np.array([35.0, 0.0, 0.0]), dt=0.02)
    alloc = _drive_usv4(cord, alloc, 0.03, 50)  # 1 s brownout
    assert alloc.isolated_mask[2]
    alloc = _drive_usv4(cord, alloc, 1.0, 200)  # 4 s healthy again
    assert not alloc.isolated_mask[2]
    assert cord.health[2] > 0.9
    assert cord.probe_count >= 1


def test_without_probe_isolation_is_permanent_until_reinstated() -> None:
    cord = ShuntingThrusterCord.usv_vectored_4(tau_actuator=0.0)
    alloc = cord.step(np.array([35.0, 0.0, 0.0]), dt=0.02)
    alloc = _drive_usv4(cord, alloc, 0.03, 50)
    alloc = _drive_usv4(cord, alloc, 1.0, 200)
    assert alloc.isolated_mask[2]  # commanded 0, so feedback can never show recovery
    cord.reinstate_actuator(2)
    alloc = _drive_usv4(cord, alloc, 1.0, 100)
    assert not alloc.isolated_mask[2]


def test_probe_keeps_dead_thruster_mostly_isolated() -> None:
    cord = ShuntingThrusterCord.usv_vectored_4(tau_actuator=0.0, probe_interval_s=2.0)
    alloc = cord.step(np.array([35.0, 0.0, 0.0]), dt=0.02)
    isolated = 0
    for _ in range(500):  # 10 s with T2 permanently at 3%
        alloc = _drive_usv4(cord, alloc, 0.03, 1)
        isolated += int(alloc.isolated_mask[2])
    assert isolated >= 480
    with pytest.raises(ValueError):
        ShuntingThrusterCord.usv_vectored_4(probe_interval_s=0.0)


def test_link_pack_does_not_mutate_caller_frame() -> None:
    link = CervicalBottleneckLink()
    frame = DescendingFrame(drive_fwd=0.5, seq=0)
    link.pack(frame)
    link.pack(frame)
    assert frame.seq == 0
    assert link.unpack(link.pack(frame)).seq == 2


def test_crc4_documented_multibit_escape_rate() -> None:
    """The README states ~6% of random corrupted words pass CRC-4; keep that claim honest."""
    rng = np.random.default_rng(0)
    words = rng.integers(0, 2**32, size=20000, dtype=np.uint64)
    accepted = 0
    for w in words:
        try:
            decode_descending_4b(int(w).to_bytes(4, "big"))
            accepted += 1
        except CervicalCRCError:
            pass
    assert 0.04 < accepted / len(words) < 0.08


# =========================================================================
# NeckCord v2.0.0 Test Suite
# =========================================================================


def test_v2_version_and_cervical_wrench_4d() -> None:
    """Verify v3.0.0 version string and 4-DOF aerial wrench decoding."""
    assert neckcord.__version__ == "3.0.0"
    f_nom = DescendingFrame(drive_fwd=0.5, turn_yaw=-0.4, strafe_lat=0.25)
    w4 = f_nom.to_wrench_4d(hover_thrust=36.0, max_pitch=3.0, max_roll=3.0, max_yaw=0.6)
    assert w4.shape == (4,)
    assert w4[0] == pytest.approx(36.0)
    assert w4[1] == pytest.approx(-0.75)
    assert w4[2] == pytest.approx(1.50)
    assert w4[3] == pytest.approx(-0.24)

    f_freeze = DescendingFrame(drive_fwd=0.8, freeze_reflex=True)
    assert np.allclose(f_freeze.to_wrench_4d(), np.zeros(4))


@pytest.mark.parametrize("dead_rotor", range(6))
def test_v2_multicopter_hex6_motor_loss_vs_unconstrained_pinv(dead_rotor: int) -> None:
    """Prove Hex6 active-set keeps u in [0, u_max] & <2% hover error where unconstrained pinv fails."""
    hex6 = ShuntingThrusterCord.multicopter_hex_6(arm_m=0.28, k_yaw=0.018, u_max=18.0)
    hex6.set_actuator_health(dead_rotor, 0.0)

    # Command hover Fz=36 N + pitch/roll demanding moment from the failed rotor's arm
    ang = 2.0 * math.pi * dead_rotor / 6.0
    tx_cmd = +1.20 * math.sin(ang)
    ty_cmd = -1.20 * math.cos(ang)
    w_des = np.array([36.0, tx_cmd, ty_cmd, 0.0], dtype=np.float64)

    # Classical unconstrained 4-DOF pseudo-inverse EVEN WITH oracle FDI dropping dead_rotor:
    w_surv = hex6.W.copy()
    w_surv[:, dead_rotor] = 0.0
    u_pinv = np.linalg.pinv(w_surv) @ w_des
    assert float(np.min(u_pinv)) < -2.0, "Unconstrained 4-DOF pinv commands illegal negative rotor thrust"

    # Physical rotors clip at u >= 0 -> naive clipping loses >50% of attitude restoration torque
    u_clipped = np.clip(u_pinv, 0.0, 18.0)
    w_clipped = w_surv @ u_clipped
    att_err_clipped = np.linalg.norm(w_clipped[1:3] - w_des[1:3]) / np.linalg.norm(w_des[1:3])
    assert att_err_clipped > 0.50

    # NeckCord v2.0 box-constrained active-set with attitude-over-yaw priority:
    res = hex6.allocate(w_des)
    assert res.commands[dead_rotor] == 0.0
    assert np.all(res.commands >= -1e-9) and np.all(res.commands <= 18.0 + 1e-9)
    w_deliv = w_surv @ res.commands
    fz_rel_err = abs(w_deliv[0] - w_des[0]) / w_des[0]
    att_rel_err = float(np.linalg.norm(w_deliv[1:3] - w_des[1:3]) / np.linalg.norm(w_des[1:3]))
    assert fz_rel_err < 0.02
    assert att_rel_err < 0.02


def test_v2_multicopter_octo8_single_and_dual_rotor_loss() -> None:
    """Prove 8-rotor [0, u_max] octocopter survives 1 and 2 dead rotors within <2% error."""
    octo = ShuntingThrusterCord.multicopter_octo_8(arm_m=0.32, k_yaw=0.018, u_max=16.0)
    w_des = np.array([44.0, 0.90, -1.10, 0.15], dtype=np.float64)

    octo.set_actuator_health(1, 0.0)
    r1 = octo.allocate(w_des)
    assert r1.commands[1] == 0.0
    assert np.all(r1.commands >= 0.0) and np.all(r1.commands <= 16.0 + 1e-9)
    assert abs(r1.predicted_wrench[0] - w_des[0]) / w_des[0] < 0.02
    assert np.linalg.norm(r1.predicted_wrench[1:3] - w_des[1:3]) / np.linalg.norm(w_des[1:3]) < 0.02

    octo.set_actuator_health(4, 0.0)
    r2 = octo.allocate(w_des)
    assert r2.commands[1] == 0.0 and r2.commands[4] == 0.0
    assert np.all(r2.commands >= 0.0) and np.all(r2.commands <= 16.0 + 1e-9)
    assert abs(r2.predicted_wrench[0] - w_des[0]) / w_des[0] < 0.02
    assert np.linalg.norm(r2.predicted_wrench[1:3] - w_des[1:3]) / np.linalg.norm(w_des[1:3]) < 0.02


def test_v2_usv_t200_asymmetric_bounds_and_deadzone() -> None:
    """BlueRobotics T200 (-40 N / +52.5 N + deadzone) respects asymmetric limits and clears deadband."""
    t200 = ShuntingThrusterCord.usv_t200_vectored_4(
        lx=0.45, ly=0.35, u_min=-40.0, u_max=52.5, deadzone=2.5
    )
    t200.set_actuator_health(2, 0.0)  # Port-Aft fouled

    w_rev = np.array([-75.0, 0.0, 10.0], dtype=np.float64)
    res_rev = t200.allocate(w_rev)
    assert res_rev.commands[2] == 0.0
    assert np.all(res_rev.commands >= -40.0 - 1e-7)
    assert np.all(res_rev.commands <= 52.5 + 1e-7)
    assert res_rev.commands[0] == pytest.approx(-40.0, abs=1e-3)
    assert res_rev.predicted_wrench[2] == pytest.approx(10.0, abs=0.15)

    # Gentle surge where symmetric 4-way split (~1.77 N) would stall inside (-2.5, +2.5) N deadzone
    t200_intact = ShuntingThrusterCord.usv_t200_vectored_4(deadzone=2.5)
    w_gentle = np.array([5.0, 0.0, 0.0], dtype=np.float64)
    res_dz = t200_intact.allocate(w_gentle)
    for u_val in res_dz.commands:
        assert u_val == 0.0 or abs(float(u_val)) >= 2.5 - 1e-7
    assert res_dz.predicted_wrench[0] == pytest.approx(5.0, abs=0.05)


def test_v2_esc_observer_broken_blade_stall_and_overcurrent() -> None:
    """Sensorless ESC observer catches broken blade (<50 ms), open-phase stall, and >1.6x weed foul."""
    obs = ESCObserver.multicopter_hex_6(u_nom=18.0, tau_motor=0.0)
    hex6 = ShuntingThrusterCord.multicopter_hex_6(u_max=18.0, tau_actuator=0.0, tau_fault=0.018)
    w_des = np.array([36.0, 0.0, -1.20, 0.0], dtype=np.float64)

    alloc = hex6.step(w_des, dt=0.005)
    detect_ms: float | None = None
    for tick in range(1, 16):
        u = alloc.commands
        rpm = obs.rpm_max * np.sqrt(np.clip(u / 18.0, 0.0, 1.0))
        cur = obs.i_idle_a + obs.k_load_a * ((rpm / obs.rpm_max) ** 3)
        # Rotor 0 shears a blade: spins at normal RPM, aerodynamic current collapses to 5% load
        cur[0] = float(obs.i_idle_a + 0.05 * obs.k_load_a * ((rpm[0] / obs.rpm_max) ** 3))

        esc_res = obs.update(u, rpm, cur, dt=0.005)
        if detect_ms is None and bool(esc_res.broken_prop_mask[0]):
            detect_ms = tick * 5.0
        alloc = hex6.step(w_des, feedback_thrust=esc_res.estimated_thrust, dt=0.005)

    assert detect_ms is not None and detect_ms < 50.0
    assert bool(alloc.isolated_mask[0]) is True
    assert alloc.commands[0] == 0.0
    assert abs(alloc.predicted_wrench[0] - 36.0) / 36.0 < 0.02
    assert abs(alloc.predicted_wrench[2] - (-1.20)) / 1.20 < 0.02

    # Also verify integrated `esc_observer` pathway on USV T200 for stall + weed-fouled overcurrent
    obs_usv = ESCObserver.usv_t200_4(tau_motor=0.0)
    usv = ShuntingThrusterCord.usv_t200_vectored_4(tau_actuator=0.0, tau_fault=0.02)
    u_cmd = np.array([35.0, 35.0, 35.0, 35.0])
    rpm_nom = float(obs_usv.rpm_max * math.sqrt(35.0 / 52.5))
    cur_nom = float(obs_usv.i_idle_a + obs_usv.k_load_a * ((rpm_nom / obs_usv.rpm_max) ** 3))

    rpm_vec = np.array([rpm_nom, 0.0, 0.62 * rpm_nom, rpm_nom])
    cur_vec = np.array([cur_nom, 0.0, 26.0, cur_nom])  # T1 open-phase stall, T2 >1.6x weed-fouled
    for _ in range(5):
        r_usv = obs_usv.update(u_cmd, rpm_vec, cur_vec, dt=0.005)

    assert r_usv.fault_codes[0] == int(ESCFaultCode.NONE)
    assert r_usv.fault_codes[1] == int(ESCFaultCode.STALL_OR_OPEN)
    assert bool(r_usv.stall_mask[1]) is True
    assert r_usv.fault_codes[2] == int(ESCFaultCode.OVERCURRENT_FOUL)
    assert bool(r_usv.overcurrent_mask[2]) is True

    step_res = usv.step(
        np.array([40.0, 0.0, 0.0]),
        esc_observer=obs_usv,
        esc_rpm=rpm_vec,
        esc_current_a=cur_vec,
        dt=0.02,
    )
    assert step_res.esc_result is not None
    assert bool(step_res.esc_result.overcurrent_mask[2]) is True


def test_v2_hexapod_leg_ik_3dof_fk_roundtrip_and_pentapod_parking() -> None:
    """Verify exact 3-DOF FK/IK roundtrip, 18-servo 1000..2000 us bounds, and +45 mm parking."""
    ik = HexapodLegIK()
    targets_mm = np.array(
        [
            [+210.0, +135.0, 0.0],
            [+195.0, -125.0, 18.0],
            [+15.0, +165.0, 0.0],
            [-10.0, -160.0, 25.0],
            [-205.0, +135.0, 0.0],
            [-190.0, -130.0, 12.0],
        ],
        dtype=np.float64,
    )
    res_ik = ik.solve_all(targets_mm)
    assert np.all(res_ik.reachable_mask)
    assert np.all((res_ik.pwm_us >= 1000) & (res_ik.pwm_us <= 2000))
    fk_mm = ik.forward_all(res_ik.angles_deg)
    assert np.allclose(fk_mm, targets_mm, atol=1e-6)

    # Integrated CPG + 18-servo IK during intact Tripod and L2-amputated Pentapod walking
    cpg = SegmentalCPGCord(adapt_to_damage=True, ik=ik)
    cmd = DescendingFrame(drive_fwd=0.85)
    for _ in range(40):
        st = cpg.step(cmd, dt=0.01)
        assert st.leg_ik is not None
        assert np.all(st.leg_ik.reachable_mask)
        assert not np.any(st.leg_ik.parked_mask)
        assert np.all((st.leg_ik.pwm_us >= 1000) & (st.leg_ik.pwm_us <= 2000))

    cpg.inject_limb_fault("L2")
    for _ in range(50):
        st = cpg.step(cmd, dt=0.01)
    assert st.active_gait == "adaptive_pentapod"
    assert st.leg_ik is not None
    assert bool(st.leg_ik.parked_mask[2]) is True
    assert st.leg_ik.foot_xyz_mm[2, 2] == pytest.approx(45.0, abs=1e-6)
    assert np.all((st.leg_ik.pwm_us >= 1000) & (st.leg_ik.pwm_us <= 2000))


def test_v2_arduino_library_structure_and_sha256_c99_parity() -> None:
    """Verify Arduino library layout, zero-FP/heap rules, and SHA-256 byte-identity with csrc/."""
    root = Path(__file__).resolve().parents[1]
    csrc = root / "csrc"
    ard = root / "arduino" / "NeckCord"

    props_text = (ard / "library.properties").read_text(encoding="utf-8")
    props = dict(
        line.split("=", 1)
        for line in props_text.splitlines()
        if "=" in line and not line.strip().startswith("#")
    )
    assert props["name"] == "NeckCord"
    assert props["version"] == "3.0.0"
    assert props["architectures"] == "*"

    sketch = (
        ard
        / "examples"
        / "ESP32_Multicopter_USV_FaultRecovery"
        / "ESP32_Multicopter_USV_FaultRecovery.ino"
    )
    assert sketch.is_file()
    assert (ard / "src" / "NeckCord.h").is_file()

    forbidden = re.compile(r"\b(float|double|malloc|calloc|realloc|free)\b")
    for rel in (
        Path("NeckCord.h"),
        Path("csrc/neckcord.h"),
        Path("csrc/neckcord.c"),
        Path("csrc/neckcord_banc_weights.h"),
    ):
        text = (ard / "src" / rel).read_text(encoding="utf-8")
        assert forbidden.findall(text) == [], f"Forbidden token in arduino/NeckCord/src/{rel}"

    for mirrored in ("neckcord.h", "neckcord.c", "neckcord_banc_weights.h"):
        h_csrc = hashlib.sha256((csrc / mirrored).read_bytes()).hexdigest()
        h_ard = hashlib.sha256((ard / "src" / "csrc" / mirrored).read_bytes()).hexdigest()
        assert h_csrc == h_ard, f"SHA-256 mismatch between csrc/{mirrored} and arduino mirror"


# =========================================================================
# NeckCord v3.0.0 Connectome & Honest Provenance Test Suite
# =========================================================================


def test_v3_provenance_tags_and_literature_gait_labels() -> None:
    """Verify honest connectome vs engineering PROVENANCE tags and literature gait labels."""
    assert SegmentalCPGCord.PROVENANCE == "connectome"
    assert ShuntingThrusterCord.PROVENANCE == "engineering"
    assert ESCObserver.PROVENANCE == "engineering"
    assert HexapodLegIK.PROVENANCE == "engineering"

    cpg = SegmentalCPGCord()
    st_tri = cpg.step(DescendingFrame(drive_fwd=0.85, gait_mode=int(GaitMode.TRIPOD)), dt=0.01)
    assert st_tri.active_gait == "tripod"
    assert st_tri.gait_provenance == "connectome"

    st_tet = cpg.step(DescendingFrame(drive_fwd=0.50, gait_mode=int(GaitMode.TETRAPOD)), dt=0.01)
    assert st_tet.active_gait == "tetrapod"
    assert st_tet.gait_provenance == "literature"

    st_wav = cpg.step(DescendingFrame(drive_fwd=0.20, gait_mode=int(GaitMode.WAVE)), dt=0.01)
    assert st_wav.active_gait == "wave"
    assert st_wav.gait_provenance == "literature"

    cpg.inject_limb_fault("L2")
    st_pent = cpg.step(DescendingFrame(drive_fwd=0.85), dt=0.01)
    assert st_pent.active_gait == "adaptive_pentapod"
    assert st_pent.gait_provenance == "connectome"


def test_v3_banc_v888_emergent_tripod_vs_maslov_sneppen_shuffled_null() -> None:
    """Prove real BANC v888 C_vnc_inh emerges Tripod (>0.88) while K=120 MS-shuffles fail (<0.60)."""
    banc = load_banc_vnc_weights()
    assert banc["C_vnc_inh"].shape == (6, 6)
    assert banc["W_dn_leg"].shape == (6, 10)

    cpg_real = SegmentalCPGCord(connectome_weights="banc_v888")
    assert cpg_real.tripod_order > 0.88  # Warm-start emergence from non-tripod symmetry break

    cmd = DescendingFrame(drive_fwd=0.85, gait_mode=int(GaitMode.TRIPOD))
    for _ in range(200):
        st_real = cpg_real.step(cmd, dt=0.01)
    assert st_real.tripod_order > 0.90

    # K = 120 (>= 100) Maslov-Sneppen degree/weight-preserving shuffled C_vnc_inh null trials
    rng = np.random.default_rng(2026)
    null_orders: list[float] = []
    for _ in range(120):
        c_shuf = maslov_sneppen_shuffle_vnc(banc["C_vnc_inh"], rng=rng, n_swaps=60)
        cpg_null = SegmentalCPGCord(
            connectome_weights={"C_vnc_inh": c_shuf, "W_dn_leg": banc["W_dn_leg"]}
        )
        for _ in range(120):
            st_null = cpg_null.step(cmd, dt=0.01)
        null_orders.append(st_null.tripod_order)

    null_mean = float(np.mean(null_orders))
    assert null_mean < 0.60
    assert st_real.tripod_order - null_mean > 0.45


def test_v3_banc_w_dn_leg_bilateral_steering_and_quadrupod_damage_respacing() -> None:
    """Verify W_dn_leg[:, 4:8] steering profile and continuous 5-leg / 4-leg damage re-spacing."""
    cpg = SegmentalCPGCord()
    assert cpg.steer_gain.shape == (6,)
    assert float(np.mean(cpg.steer_gain)) == pytest.approx(0.65, abs=1e-6)
    # In BANC v888, DNa02+DNa01 recruit pro/mesothoracic T1/T2 more strongly than metathoracic T3
    assert float(np.mean(cpg.steer_gain[:4])) > float(np.mean(cpg.steer_gain[4:]))

    # Diagonal 2-leg loss (L1 + R3 -> 4 surviving legs: R1, L2, R2, L3) re-spaces into 4 quadrants
    cpg_quad = SegmentalCPGCord(adapt_to_damage=True)
    cpg_quad_unadapted = SegmentalCPGCord(adapt_to_damage=False)
    for cord in (cpg_quad, cpg_quad_unadapted):
        cord.inject_limb_fault("L1")
        cord.inject_limb_fault("R3")

    cmd = DescendingFrame(drive_fwd=0.80)
    for _ in range(180):
        cpg_quad.step(cmd, dt=0.01)
        cpg_quad_unadapted.step(cmd, dt=0.01)

    quad_stable = 0
    unadapted_stable = 0
    min_grounded_adapt = 6
    min_grounded_unadapt = 6
    for _ in range(300):
        st_q = cpg_quad.step(cmd, dt=0.01)
        st_u = cpg_quad_unadapted.step(cmd, dt=0.01)
        assert st_q.active_gait == "adaptive_quadrupod"
        assert not st_q.stance_mask[0] and not st_q.stance_mask[5]
        min_grounded_adapt = min(min_grounded_adapt, int(np.sum(st_q.stance_mask)))
        min_grounded_unadapt = min(min_grounded_unadapt, int(np.sum(st_u.stance_mask)))
        if st_q.is_statically_stable:
            quad_stable += 1
        if st_u.is_statically_stable:
            unadapted_stable += 1

    # Re-spaced 4-leg quadrupod maintains >= 3 grounded feet 100% of the cycle (~70% polygon stability
    # without active body-CoM sway, vs collapsing to 1 grounded foot and ~4% stability unadapted)
    assert min_grounded_adapt >= 3
    assert min_grounded_unadapt <= 2
    assert quad_stable / 300.0 >= 0.65
    assert unadapted_stable / 300.0 < 0.15
    surv_sorted = np.sort(np.mod(cpg_quad.phases[[1, 2, 3, 4]] - cpg_quad.phases[1], 2.0 * math.pi))
    surv_gaps_deg = np.rad2deg(np.diff(np.append(surv_sorted, 2.0 * math.pi)))
    assert float(np.min(surv_gaps_deg)) > 60.0


def test_v3_python_c99_banc_weights_and_dynamics_parity() -> None:
    """Verify exact Q12 weights parity between banc_vnc_v3.npz and csrc/neckcord_banc_weights.h."""
    root = Path(__file__).resolve().parents[1]
    hdr_h = (root / "csrc" / "neckcord.h").read_text(encoding="utf-8")
    assert "#define NC_CORD_VERSION_MAJOR 3" in hdr_h
    assert '#include "neckcord_banc_weights.h"' in hdr_h

    weights_h = (root / "csrc" / "neckcord_banc_weights.h").read_text(encoding="utf-8")
    init_body = weights_h.split("k_nc_banc_vnc_default", 1)[1]
    blocks = re.findall(r"\{([^{}]+)\}", init_body)
    # Extract C_vnc_inh_q12 (36 uint16) and W_dn_leg_q12 (60 int16)
    c_tokens = [int(tok.rstrip("u")) for tok in re.findall(r"-?\d+u?", blocks[0])]
    w_tokens = [int(tok) for tok in re.findall(r"-?\d+", blocks[1])]
    assert len(c_tokens) == 36
    assert len(w_tokens) == 60

    banc = load_banc_vnc_weights()
    assert np.array_equal(np.array(c_tokens, dtype=np.uint16).reshape(6, 6), banc["C_vnc_inh_q12"])
    assert np.array_equal(np.array(w_tokens, dtype=np.int16).reshape(6, 10), banc["W_dn_leg_q12"])

    # Verify C99 Q12/BAM warm-start phases match Python warm-start phases within < 0.5 deg
    c_q12 = banc["C_vnc_inh_q12"].astype(np.int64)
    phi_bam = np.array([0, 4694, 8866, 1565, 3129, 7301], dtype=np.int64)
    for _ in range(15):
        next_bam = np.zeros(6, dtype=np.int64)
        for i in range(6):
            acc = 0
            for j in range(6):
                if i == j:
                    continue
                ang_rad = ((phi_bam[i] - phi_bam[j]) & 0xFFFF) * (2.0 * math.pi / 65536.0)
                s_q15 = int(round(math.sin(ang_rad) * 32767.0))
                acc += (67797 * int(c_q12[i, j]) * s_q15) // (1 << 27)
            next_bam[i] = (phi_bam[i] + (acc * 20) // 1000) & 0xFFFF
        phi_bam = next_bam
    phi_bam = (phi_bam - phi_bam[0]) & 0xFFFF
    phi_c_rad = phi_bam * (2.0 * math.pi / 65536.0)

    cpg_py = SegmentalCPGCord()
    max_deg_diff = max(
        math.degrees(_circ_dist(float(cpg_py.phases[i]), float(phi_c_rad[i]))) for i in range(6)
    )
    assert max_deg_diff < 0.5
    assert abs(cpg_py.tripod_order - compute_tripod_order(phi_c_rad)) < 0.005
