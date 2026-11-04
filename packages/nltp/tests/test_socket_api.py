"""
End-to-End OS UDP Socket, Late-Joiner, Multi-Client Server, Asyncio, and >127-ch Tensor Tests.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

torch = pytest.importorskip("torch", reason="optional: tensor tests need torch (pip install torch)")

from necklink import AsyncNeckSocket, NeckSocket, NeckUDPServer
from necklink.cli import main as cli_main


def test_real_os_udp_zero_config_dict_streaming():
    with NeckSocket.udp_receiver("127.0.0.1", 0, profile="sensor_plateau") as rx:
        host, port = rx.getsockname()
        with NeckSocket.udp_sender(host, port, profile="sensor_plateau") as tx:
            last_out = None
            for t in range(300):
                # Steady cruise for most ticks + a sharp H2S gas spike around t=150
                h2s = 0.05 + (14.5 * np.exp(-0.5 * ((t - 150) / 6.0) ** 2))
                payload = {
                    "rpm": 1200.0 + 2.0 * np.sin(t * 0.05),
                    "ph": 8.12,
                    "h2s_ppm": float(h2s),
                    "temp_c": 29.4,
                }
                tx.send(payload)
                out = rx.recv_tick()
                if out is not None:
                    last_out = out

            assert isinstance(last_out, dict)
            assert set(last_out.keys()) == {"rpm", "ph", "h2s_ppm", "temp_c"}
            assert abs(last_out["ph"] - 8.12) < 0.25
            st = tx.stats()
            assert st["silent_tick_pct"] > 50.0, f"Expected >50% silent ticks on calm dict stream, got {st['silent_tick_pct']}%"
            assert st["compression_x"] > 3.5


def test_late_joiner_bootstrap_without_handshake():
    """Receiver misses the first 45 transmitted packets completely and still self-synchronizes!"""
    wire_queue: list[bytes] = []
    pkt_counter = [0]

    def drop_early_wire(b: bytes) -> None:
        pkt_counter[0] += 1
        # Simulate receiver offline for first 25 packets, then 30% packet loss!
        if pkt_counter[0] > 25 and (pkt_counter[0] % 3 != 0):
            wire_queue.append(b)

    tx = NeckSocket(profile="sensor_plateau", beacon_interval_pkts=10, send_fn=drop_early_wire)
    rx = NeckSocket(profile="sensor_plateau", recv_fn=lambda: wire_queue.pop(0) if wire_queue else None)

    synced_val = None
    for t in range(240):
        val = {"wave_m": float(1.5 * np.sin(t * 0.12)), "wind_ms": 6.5}
        tx.send(val)
        out = rx.recv_tick()
        if out is not None:
            synced_val = out

    assert isinstance(synced_val, dict), "Late-joining receiver failed to bootstrap from periodic sync beacon"
    assert abs(synced_val["wind_ms"] - 6.5) < 0.5


def test_multi_client_udp_fleet_server():
    with NeckUDPServer(("127.0.0.1", 0), profile="sensor_plateau") as srv:
        host, port = srv.getsockname()
        with (
            NeckSocket.udp_sender(host, port, profile="sensor_plateau") as buoy1,
            NeckSocket.udp_sender(host, port, profile="sensor_plateau") as buoy2,
            NeckSocket.udp_sender(host, port, profile="sensor_plateau") as aqms1,
        ):
            snap = {}
            for t in range(40):
                buoy1.send({"id": 1.0, "heave": float(np.sin(t * 0.2))})
                buoy2.send({"id": 2.0, "heave": float(np.cos(t * 0.2))})
                aqms1.send([10.0, 22.5, 45.0])
                snap = srv.poll_tick()

            assert len(srv.peers) == 3
            assert len(snap) == 3


def test_large_tensor_multilane_striping_over_udp():
    """Tests >127 channels (160-dim PyTorch tensor striped across 3 lanes) over real OS UDP."""
    with NeckSocket.udp_receiver("127.0.0.1", 0, profile="split_ai") as rx:
        host, port = rx.getsockname()
        with NeckSocket.udp_sender(host, port, profile="split_ai") as tx:
            base = torch.linspace(-2.0, 2.0, 160)
            out_t = None
            for step in range(30):
                z = base + 0.15 * torch.sin(torch.arange(160, dtype=torch.float32) * 0.1 + step * 0.2)
                tx.send(z)
                res = rx.recv_tick(as_tensor=True)
                if res is not None:
                    out_t = res

            assert isinstance(out_t, torch.Tensor)
            assert out_t.shape == (160,)
            rmse = float(torch.sqrt(torch.mean((out_t - z) ** 2)))
            assert rmse < 0.35


def test_asyncio_udp_socket_and_cli(tmp_path: Path):
    async def _run_async() -> None:
        rx = await AsyncNeckSocket.create(local_addr=("127.0.0.1", 0), profile="auto")
        assert rx._transport is not None
        sockname = rx._transport.get_extra_info("sockname")
        tx = await AsyncNeckSocket.create(remote_addr=(sockname[0], sockname[1]), profile="auto")
        try:
            for k in range(25):
                tx.send({"v": float(k * 0.5), "steady": 42.0})
                await asyncio.sleep(0.002)
            out = rx.recv_tick()
            assert isinstance(out, dict)
            assert abs(out["steady"] - 42.0) < 1.0
        finally:
            tx.close()
            rx.close()

    asyncio.run(_run_async())

    # CLI smoke tests
    rc_demo = cli_main(["demo", "--workload", "aqms", "--steps", "300", "--loss", "0.35"])
    assert rc_demo == 0
    out_h = tmp_path / "custom12.h"
    rc_c = cli_main(["export-c", "--channels", "12", "--prefix", "my_sensor", "--out", str(out_h)])
    assert rc_c == 0 and out_h.exists()
