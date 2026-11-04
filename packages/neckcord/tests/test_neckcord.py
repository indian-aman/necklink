"""Comprehensive test suite for `neckcord` v1.1.0."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import numpy as np
import pytest

from neckcord import (
    CervicalBottleneckLink,
    CervicalCRCError,
    DescendingFrame,
    GaitMode,
    LIMB_NAMES,
    SegmentalCPGCord,
    ShuntingThrusterCord,
    decode_descending_4b,
    encode_descending_4b,
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
    """Perturbed oscillator phases converge to strict Tripod anti-phase lock at fast speed."""
    rng = np.random.default_rng(7)
    cpg = SegmentalCPGCord()
    cpg.phases = rng.uniform(0.0, 2.0 * math.pi, size=6)

    for _ in range(250):
        st = cpg.step(0.90, dt=0.01)

    assert st.active_gait == "tripod"
    # Tripod A: {0=L1, 3=R2, 4=L3}; Tripod B: {1=R1, 2=L2, 5=R3}
    assert _circ_dist(st.phases[0], st.phases[3]) < 0.08
    assert _circ_dist(st.phases[0], st.phases[4]) < 0.08
    assert abs(_circ_dist(st.phases[0], st.phases[1]) - math.pi) < 0.08
    assert abs(_circ_dist(st.phases[2], st.phases[3]) - math.pi) < 0.08


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
