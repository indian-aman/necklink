"""Comprehensive test suite for `necklink-cns` (`v3.0.0`)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np
import pytest

from necklink_cns import (
    BANC_V888_PRIORS,
    MavHeartbeat,
    MavLink2Codec,
    MavLinkUdpBridge,
    MavManualControl,
    MavObstacleDistance,
    MavScaledImu,
    MavStatusText,
    NeckLinkCNS,
    NeckLinkROS2Adapter,
    __version__,
)
from necklink_cns.cli import main as cli_main
from necklink_cns.connectome import _load_default_dn_bundle


def _strip_c_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def test_version_and_banc_v888_priors() -> None:
    assert __version__ == "3.0.0"
    assert BANC_V888_PRIORS["total_neurons"] == 157789
    assert BANC_V888_PRIORS["cell_counts"]["DN_all"] == 1316
    assert BANC_V888_PRIORS["cell_counts"]["AN_all"] == 1849
    ratios = BANC_V888_PRIORS["derived_biophysical_ratios"]
    assert 1.90 < ratios["optic_t5_to_t4_lplc2_off_dominance"] < 2.05
    assert 3.5 < ratios["mb_alpn_to_kc_claws_mean"] < 4.5


def test_mavlink_v2_wire_roundtrips_and_udp_bridge() -> None:
    codec = MavLink2Codec(sysid=42, compid=191)
    p1 = codec.encode_heartbeat(MavHeartbeat(custom_mode=7))
    p2 = codec.encode_manual_control(MavManualControl(x=750, y=-300, z=500, r=420, buttons=1, target=0))
    p3 = codec.encode_scaled_imu(MavScaledImu(1000, 10, -20, 995, 5, -6, 120, 310, -40, 820))
    p4 = codec.encode_statustext("NECKLINK LOOM DODGE")
    p5 = codec.encode_obstacle_distance(MavObstacleDistance(time_usec=999999, distances_cm=tuple([350] * 72)))

    # Concatenate with Leading garbage bytes to verify streaming resync + zero-trim decoding
    stream = b"\x00\xFFgarbage" + p1 + p2 + p3 + p4 + p5
    frames = codec.feed(stream)
    assert len(frames) == 5
    assert MavHeartbeat.from_payload(frames[0].payload).custom_mode == 7
    mc = MavManualControl.from_payload(frames[1].payload)
    assert (mc.x, mc.y, mc.r, mc.target) == (750, -300, 420, 0)
    imu = MavScaledImu.from_payload(frames[2].payload)
    assert (imu.zacc_mg, imu.zmag_mgauss) == (995, 820)
    st = MavStatusText.from_payload(frames[3].payload)
    assert st.text == "NECKLINK LOOM DODGE"
    od = MavObstacleDistance.from_payload(frames[4].payload)
    assert od.distances_cm[0] == 350

    with MavLinkUdpBridge() as a, MavLinkUdpBridge() as b:
        a.send_packet(p2, b.local_addr)
        rx = []
        for _ in range(20):
            rx.extend(b.poll_frames())
            if rx:
                break
        assert len(rx) == 1
        assert MavManualControl.from_payload(rx[0].payload).x == 750


def test_closed_loop_pole_dodge_and_hexacopter_fault() -> None:
    cns = NeckLinkCNS(vehicle="hexacopter", grid_size=32)
    yy, xx = np.mgrid[0:32, 0:32]
    bg = 155.0 + 18.0 * np.sin(xx * 0.35) * np.cos(yy * 0.35)

    dodged_right = False
    for k in range(24):
        frame = bg.copy()
        if k >= 5:
            hw = 1 + (k - 5) // 3
            frame[:, max(0, 8 - hw) : min(32, 8 + hw + 1)] = 8.0  # Left vertical pole
        # Simulate broken blade on Rotor #2 via ESC RPM + Current
        cmds = np.maximum(0.0, cns._last_cmds)
        rpm = np.sqrt(cmds / max(1e-6, cns.cord.u_max)) * 9000.0
        curr = 0.40 + 18.0 * ((rpm / 9000.0) ** 3)
        if k >= 6:
            curr[2] = 0.45  # Snapped blade -> near-idle current despite normal RPM!
        out = cns.step(
            dt=0.02,
            speed_m_s=1.2,
            gyro_xyz=np.array([0.02, -0.01, 0.0]),
            accel_xyz=np.array([0.2, -0.1, 9.80]),
            mag_xyz=np.array([0.45, 0.0, 0.89]),
            camera_frame=frame,
            esc_rpm=rpm,
            esc_current_a=curr,
        )
        assert np.all(out.actuator_commands >= -1e-9), "Unidirectional hexacopter must never command < 0!"
        if out.descending.behavior_mode in ("DODGE_LOOM", "MOONWALK_REVERSE"):
            dodged_right = out.descending.turn_yaw < 0.0

    assert dodged_right
    assert out.actuator_health[2] < 0.65


def test_ros2_adapter_and_continual_chemical_memory() -> None:
    cns = NeckLinkCNS(vehicle="usv_t200", grid_size=32, n_chem_channels=8)
    gas_a = np.array([1.2, 0.1, 0.2, 1.5, 0.1, 0.05, 1.1, 0.1])
    gas_b = np.array([0.05, 1.4, 1.3, 0.1, 1.6, 1.2, 0.05, 1.4])
    cns.learn_chemical(gas_a, class_id=1, consolidate=True)
    for _ in range(8):
        cns.learn_chemical(gas_b, class_id=2, consolidate=False)

    adapter = NeckLinkROS2Adapter(cns)
    adapter.on_imu(np.array([0.01, -0.02, 0.05]), np.array([0.2, -0.1, 9.8]))
    adapter.on_mag(np.array([0.45, 0.05, 0.88]))
    adapter.on_chemical(gas_a * 4.0)  # 4x concentration scale test!
    adapter.on_vibration_window(np.sin(np.linspace(0, 16.0 * np.pi, 128)) * 200.0)

    res = adapter.step_camera(np.full((32, 32), 120.0, dtype=np.float64), speed_m_s=1.0, dt=0.02)
    assert res["cns_output"].predicted_class == 1
    assert isinstance(res["twist"].linear_x, float)
    assert cli_main(["--version"]) == 0
    assert cli_main(["demo"]) == 0


def test_banc_dn_causality_and_v2_fallback() -> None:
    yy, xx = np.mgrid[0:32, 0:32]
    bg = 155.0 + 18.0 * np.sin(xx * 0.35) * np.cos(yy * 0.35)

    cns_real = NeckLinkCNS(vehicle="usv_t200", grid_size=32, connectome_weights="banc_v888")
    cns_v2 = NeckLinkCNS(vehicle="usv_t200", grid_size=32, connectome_weights="v2_analytic")

    dn_mirror = _load_default_dn_bundle()
    w_leg_mirror = dn_mirror["W_dn_leg"].copy()
    w_leg_mirror[[0, 1, 2, 3, 4, 5]] = w_leg_mirror[[1, 0, 3, 2, 5, 4]]
    dn_mirror["W_dn_leg"] = w_leg_mirror
    cns_mirror = NeckLinkCNS(
        vehicle="usv_t200",
        grid_size=32,
        connectome_weights={"necklink_cns": dn_mirror},
    )

    assert cns_real.arbitrator.PROVENANCE == "connectome"
    assert cns_v2.arbitrator.PROVENANCE == "v2_analytic"

    out_real = out_mirror = out_v2 = None
    for k in range(22):
        frame = bg.copy()
        if k >= 5:
            hw = 1 + (k - 5) // 3
            frame[:, max(0, 8 - hw) : min(32, 8 + hw + 1)] = 8.0  # Left obstacle -> must dodge right (turn_yaw < 0)
        kw = dict(
            dt=0.02,
            speed_m_s=1.2,
            gyro_xyz=np.zeros(3),
            accel_xyz=np.array([0.0, 0.0, 9.81]),
            mag_xyz=np.array([0.45, 0.0, 0.89]),
            camera_frame=frame,
        )
        out_real = cns_real.step(**kw)
        out_mirror = cns_mirror.step(**kw)
        out_v2 = cns_v2.step(**kw)

    assert out_real is not None and out_mirror is not None and out_v2 is not None
    assert out_real.descending.turn_yaw < 0.0
    assert out_mirror.descending.turn_yaw > 0.0  # Swapping L/R leg MN rows flips dodge direction!
    assert out_v2.descending.turn_yaw < 0.0


def test_c99_zero_float_zero_heap_and_arduino_sha256_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    csrc = root / "csrc"
    ard = root / "arduino" / "NeckLinkCNS"
    assert (ard / "library.properties").is_file()
    assert (ard / "src" / "NeckLinkCNS.h").is_file()

    forbidden = re.compile(r"\b(float|double|malloc|calloc|realloc|free)\b")
    top_files = (
        "nl_mavlink.h",
        "nl_mavlink.c",
        "necklink_cns.h",
        "necklink_cns.c",
        "necklink_cns_banc_weights.h",
    )
    organ_files = (
        "necksight.h",
        "necksight.c",
        "necksight_banc_weights.h",
        "neckcompass.h",
        "neckcompass.c",
        "neckcompass_banc_weights.h",
        "neckcord.h",
        "neckcord.c",
        "neckcord_banc_weights.h",
        "necksense.h",
        "necksense.c",
        "necksense_banc_weights.h",
    )
    for fname in top_files:
        p_csrc = csrc / fname
        p_ard = ard / "src" / "csrc" / fname
        assert hashlib.sha256(p_csrc.read_bytes()).hexdigest() == hashlib.sha256(p_ard.read_bytes()).hexdigest()
        hits = forbidden.findall(_strip_c_comments(p_csrc.read_text(encoding="utf-8")))
        assert hits == [], f"Forbidden tokens {hits} in {fname}"

    for fname in organ_files:
        p_csrc = csrc / "organs" / fname
        p_ard = ard / "src" / "csrc" / fname
        assert hashlib.sha256(p_csrc.read_bytes()).hexdigest() == hashlib.sha256(p_ard.read_bytes()).hexdigest()
        hits = forbidden.findall(_strip_c_comments(p_csrc.read_text(encoding="utf-8")))
        assert hits == [], f"Forbidden tokens {hits} in organs/{fname}"


def test_readme_python_examples_execute() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
    assert len(blocks) >= 2
    for block in blocks:
        ns: dict[str, object] = {}
        exec(compile(block, "<README.md>", "exec"), ns, ns)
