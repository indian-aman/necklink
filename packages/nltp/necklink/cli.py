"""
Command-Line Interface for NeckLink & Internet-Ready NLTP (`python -m necklink ...`).

Subcommands:
  - `demo`      : Live OS UDP loopback telemetry demo under simulated packet loss
  - `export-c`  : Generate zero-float, zero-malloc ANSI C99 header for ESP32/STM32/C++
  - `gen-key`   : Generate a fresh 256-bit `ChaCha20-Poly1305` key for `--psk`
  - `stun`      : Live `RFC 5389` WAN NAT/RTT check against Cloudflare & Google STUN
  - `relay`     : Run a 1-port stateless zero-knowledge `NLTPRelayHub` for 4G Symmetric CGNATs
  - `serve`     : Run an Internet-ready `NLTPServer` (direct DNS/IP, STUN P2P, or `--relay`)
  - `send-file` : Send a `SHA-256` verified file over the Internet (`--psk`, `--relay`, `--max-kbps`)
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional, Sequence, Tuple

import numpy as np

from .c_export import export_necklink_c99_header
from .internet import NLTPRelayHub, discover_wan_endpoint, generate_nltp_key
from .nltp import NLTPClient, NLTPServer
from .socket_api import NeckSocket, _profile_to_config
from .workloads import generate_aqms_gas_plume_telemetry, generate_marine_buoy_usv_telemetry


def _parse_host_port(spec: Optional[str]) -> Optional[Tuple[str, int]]:
    if not spec:
        return None
    s = spec.strip()
    if s.startswith("[") and "]:" in s:
        h, p = s[1:].rsplit("]:", 1)
        return (h, int(p))
    if ":" not in s:
        raise ValueError(f"Expected host:port, got {spec!r}")
    h, p = s.rsplit(":", 1)
    return (h, int(p))


def _mask_ip(ip_str: str) -> str:
    if ":" in ip_str:
        return "xxxx:xxxx::xxxx"
    return "xxx.xxx.xxx.xxx"


def cmd_demo(args: argparse.Namespace) -> int:
    rng = np.random.default_rng(args.seed)
    if args.workload == "aqms":
        x, meta = generate_aqms_gas_plume_telemetry(n_steps=args.steps, seed=args.seed)
        profile = "sensor_plateau"
        names = ["H2S", "NH3", "SO2", "NO2", "VOC", "PM2.5", "PM10", "CO", "O3", "Temp", "RH", "Wind"]
    else:
        x, meta = generate_marine_buoy_usv_telemetry(n_steps=args.steps, seed=args.seed)
        profile = "sensor_plateau"
        names = [f"ch{i}" for i in range(x.shape[1])]

    with NeckSocket.udp_receiver("127.0.0.1", 0, profile=profile) as rx:
        host, port = rx.getsockname()
        import socket as _os_sock

        udp_tx = _os_sock.socket(_os_sock.AF_INET, _os_sock.SOCK_DGRAM)

        def lossy_udp_wire(pkt: bytes) -> None:
            if rng.random() >= args.loss:
                udp_tx.sendto(pkt, (host, port))

        tx = NeckSocket(profile=profile, send_fn=lossy_udp_wire)
        rec = np.zeros_like(x)

        for t in range(len(x)):
            payload = {names[i]: float(x[t, i]) for i in range(x.shape[1])}
            tx.send(payload)
            out = rx.recv_tick()
            if isinstance(out, dict):
                rec[t] = [out[k] for k in names]

        udp_tx.close()

    w = max(10, len(x) // 10)
    std_ch = np.maximum(np.std(x[w:], axis=0), 1e-6)
    nrmse_pct = float(100.0 * np.mean(np.sqrt(np.mean(((x[w:] - rec[w:]) / std_ch) ** 2, axis=0))))
    st_tx = tx.stats()
    st_rx = rx.stats()

    print(f"=== NeckLink Live OS UDP Socket Demo ({meta['name']}) ===")
    print(f"  Endpoint        : udp://{host}:{port}  (Channels={x.shape[1]}, Steps={len(x)}, Loss={args.loss*100:.0f}%)")
    print(f"  Raw Float32 UDP : {int(st_tx['raw_equiv_bytes']):,} bytes")
    print(f"  NeckSocket Wire : {int(st_tx['wire_bytes_sent']):,} bytes  ->  {st_tx['compression_x']:.2f}x smaller ({st_tx['silent_tick_pct']:.1f}% silent ticks)")
    print(f"  RX Self-Healing : {int(st_rx['rx_packets'])} pkts rcvd | {int(st_rx['detected_lost_packets'])} lost | {int(st_rx['recovered_echo_packets'])} cryptic echoes healed")
    print(f"  Waveform NRMSE  : {nrmse_pct:.2f}%  (0 ms head-of-line stall latency)")
    return 0


def cmd_export_c(args: argparse.Namespace) -> int:
    cfg = _profile_to_config(args.channels, profile=args.profile)
    hdr_path, max_pkt = export_necklink_c99_header(cfg, Path(args.out), prefix=args.prefix)
    print(f"Generated zero-float ANSI C99 codec header: {hdr_path.resolve()} (max_pkt={max_pkt} B)")
    print(f"Pair with csrc/necklink_socket.h (`#define NL_CODEC_PREFIX {args.prefix}`) for drop-in UDP/MCU sockets.")
    return 0


def cmd_gen_key(_args: argparse.Namespace) -> int:
    print(generate_nltp_key())
    return 0


def cmd_stun(args: argparse.Namespace) -> int:
    res = discover_wan_endpoint(timeout_s=args.timeout)
    shown_ip = res.public_ip if args.show_ip else _mask_ip(res.public_ip)
    print("=== NLTP Live Internet STUN & NAT Reachability Check ===")
    print(f"  Local UDP Port  : {res.local_port}")
    print(f"  Mapped WAN Port : {shown_ip}:{res.public_port}" + ("" if args.show_ip else "  (pass --show-ip to unmask)"))
    print(f"  Internet RTT    : {res.rtt_ms:.2f} ms  ({', '.join(res.servers_queried)})")
    print(f"  NAT Verdict     : {res.nat_summary}")
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    with NLTPRelayHub((args.host, args.port)) as hub:
        h, p = hub.getsockname()[:2]
        print(f"NLTP Zero-Knowledge CGNAT Relay Hub listening on udp://{h}:{p}")
        t_end = time.perf_counter() + args.duration if args.duration > 0 else float("inf")
        try:
            while time.perf_counter() < t_end:
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        print(f"Relay closing (forwarded {hub.forwarded_packets:,} encrypted packets, {hub.forwarded_bytes:,} B)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    relay_ep = _parse_host_port(args.relay)
    with NLTPServer(
        (args.host, args.port),
        save_dir=args.save_dir,
        psk=args.psk,
        relay_addr=relay_ep,
        room=args.room,
        max_tx_kbps=args.max_kbps,
        loss_drop_sim=args.loss,
    ) as srv:
        h, p = srv.getsockname()[:2]
        enc_str = "ChaCha20-Poly1305" if args.psk else "PLAINTEXT (LAN/dev)"
        mode_str = f"via relay {args.relay} [room={args.room}]" if relay_ep else f"direct udp://{h}:{p}"
        print(f"NLTP v1 Server [{enc_str}] listening {mode_str} (save_dir={Path(args.save_dir).resolve()})")
        count = 0
        t_end = time.perf_counter() + args.duration if args.duration > 0 else float("inf")
        while time.perf_counter() < t_end:
            item = srv.recv(timeout=0.5)
            if item is not None:
                count += 1
                print(
                    f"[{count}] kind={item.kind} bytes={len(item.data):,} sha256_ok={item.sha256_verified} "
                    f"file={item.saved_path or item.filename or '-'}"
                )
                if args.max_items > 0 and count >= args.max_items:
                    break
    return 0


def cmd_send_file(args: argparse.Namespace) -> int:
    relay_ep = _parse_host_port(args.relay)
    target = None if relay_ep else (args.host, args.port)
    with NLTPClient(
        target,
        psk=args.psk,
        relay_addr=relay_ep,
        room=args.room,
        max_tx_kbps=args.max_kbps,
        loss_drop_sim=args.loss,
    ) as cli:
        receipt = cli.send_file(args.file, timeout=args.timeout)
        print(
            f"File={receipt['filename']} ({receipt['bytes']:,} B) | Encrypted={receipt['encrypted']} | "
            f"SHA256_OK={receipt['verified_sha256']} | Time={receipt['elapsed_s']:.3f}s | "
            f"Goodput={receipt['goodput_kbps']:.1f} kbps | EstLoss={receipt['estimated_link_loss_pct']:.1f}%"
        )
        return 0 if receipt["verified_sha256"] else 1


def cmd_products(_args: argparse.Namespace) -> int:
    from .suite import NECKLINK_SUITE_CATALOG, check_suite_installed

    installed = check_suite_installed()
    print("=== NeckLink Fruit-Fly Brain Product Suite ===")
    for item in NECKLINK_SUITE_CATALOG:
        ver = installed.get(item["pypi"], "not-installed")
        print(
            f"  [{item['name']:11s}] ({item['pypi']:11s} -> {ver:13s}) | {item['region']}\n"
            f"                {item['summary']}\n"
            f"                Repo: {item['repo']}  |  Install: pip install {item['pypi']}"
        )
    print("  [Umbrella   ] pip install necklink  -> installs all 5 modules")
    return 0


def cmd_suite_demo(args: argparse.Namespace) -> int:
    import json as _json
    from .suite import run_autonomous_reflex_demo

    res = run_autonomous_reflex_demo(loss=args.loss, seed=args.seed)
    print("=== NeckLink 5-Organ Closed-Loop Drosophila Reflex Demo ===")
    print(_json.dumps(res, indent=2))
    ok = (
        res["nltp_reflex_received"]
        and res["nltp_4b_command_received"]
        and res["nltp_file_sha256_verified"]
        and not res["sight_glare_50x_false_alarm"]
        and res["sight_looming_triggered"]
    )
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="necklink", description="NeckLink Zero-Retransmission Neural Socket, Internet NLTP & QUIC Adapter")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="Run live OS UDP loopback socket demo under packet loss")
    p_demo.add_argument("--workload", choices=["buoy", "aqms"], default="aqms")
    p_demo.add_argument("--steps", type=int, default=1200)
    p_demo.add_argument("--loss", type=float, default=0.40, help="Simulated UDP packet loss fraction (0.0..0.8)")
    p_demo.add_argument("--seed", type=int, default=42)
    p_demo.set_defaults(func=cmd_demo)

    p_exp = sub.add_parser("export-c", help="Generate zero-float ANSI C99 header for embedded/C/C++ apps")
    p_exp.add_argument("--channels", type=int, required=True, help="Number of channels (1..127)")
    p_exp.add_argument("--profile", choices=["auto", "sensor_plateau", "fast_spike", "split_ai", "ultra_low_bytes"], default="auto")
    p_exp.add_argument("--prefix", type=str, default="necklink_node")
    p_exp.add_argument("--out", type=str, required=True, help="Output .h file path")
    p_exp.set_defaults(func=cmd_export_c)

    p_gk = sub.add_parser("gen-key", help="Generate a 256-bit ChaCha20-Poly1305 pre-shared key for --psk")
    p_gk.set_defaults(func=cmd_gen_key)

    p_stun = sub.add_parser("stun", help="Run live RFC 5389 WAN STUN & NAT Cone/CGNAT reachability check")
    p_stun.add_argument("--timeout", type=float, default=2.5)
    p_stun.add_argument("--show-ip", action="store_true", help="Print unmasked external WAN IP address")
    p_stun.set_defaults(func=cmd_stun)

    p_rel = sub.add_parser("relay", help="Start stateless zero-knowledge NLTPRelayHub for 4G Symmetric CGNAT traversal")
    p_rel.add_argument("--host", type=str, default="0.0.0.0")
    p_rel.add_argument("--port", type=int, default=9000)
    p_rel.add_argument("--duration", type=float, default=0.0, help="Auto-exit after N seconds (0=forever)")
    p_rel.set_defaults(func=cmd_relay)

    p_srv = sub.add_parser("serve", help="Run Internet-ready NLTP v1 UDP server for files, messages, reflexes & streams")
    p_srv.add_argument("--host", type=str, default="0.0.0.0")
    p_srv.add_argument("--port", type=int, default=9000)
    p_srv.add_argument("--save-dir", type=str, default="./nltp_inbox")
    p_srv.add_argument("--psk", type=str, default=None, help="ChaCha20-Poly1305 pre-shared key or passphrase")
    p_srv.add_argument("--relay", type=str, default=None, help="Optional NLTPRelayHub host:port for 4G CGNAT traversal")
    p_srv.add_argument("--room", type=str, default=None, help="Relay room name when --relay is set")
    p_srv.add_argument("--max-kbps", type=float, default=50000.0, help="Router-safe max TX rate in kbps")
    p_srv.add_argument("--loss", type=float, default=0.0)
    p_srv.add_argument("--duration", type=float, default=0.0, help="Auto-exit after N seconds (0=forever)")
    p_srv.add_argument("--max-items", type=int, default=0, help="Auto-exit after receiving N items")
    p_srv.set_defaults(func=cmd_serve)

    p_sf = sub.add_parser("send-file", help="Send a SHA-256 verified file to an NLTP v1 server over LAN or Internet")
    p_sf.add_argument("--host", type=str, default="127.0.0.1")
    p_sf.add_argument("--port", type=int, default=9000)
    p_sf.add_argument("--file", type=str, required=True)
    p_sf.add_argument("--psk", type=str, default=None, help="ChaCha20-Poly1305 pre-shared key or passphrase")
    p_sf.add_argument("--relay", type=str, default=None, help="Optional NLTPRelayHub host:port for 4G CGNAT traversal")
    p_sf.add_argument("--room", type=str, default=None, help="Relay room name when --relay is set")
    p_sf.add_argument("--max-kbps", type=float, default=50000.0, help="Router-safe max TX rate in kbps")
    p_sf.add_argument("--loss", type=float, default=0.0)
    p_sf.add_argument("--timeout", type=float, default=60.0)
    p_sf.set_defaults(func=cmd_send_file)

    p_prod = sub.add_parser("products", help="List all 5 Drosophila CNS-inspired packages in the NeckLink brand suite")
    p_prod.set_defaults(func=cmd_products)

    p_suite = sub.add_parser("suite-demo", help="Run live 5-organ closed-loop Drosophila autonomy & reflex mission over NLTP")
    p_suite.add_argument("--loss", type=float, default=0.15, help="Simulated UDP packet loss fraction (0.0..0.5)")
    p_suite.add_argument("--seed", type=int, default=42)
    p_suite.set_defaults(func=cmd_suite_demo)

    parsed = parser.parse_args(argv)
    return int(parsed.func(parsed))


if __name__ == "__main__":
    raise SystemExit(main())
