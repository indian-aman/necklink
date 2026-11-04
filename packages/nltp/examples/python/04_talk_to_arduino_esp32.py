"""
Example 4: Talking to an Arduino / ESP32 Board Running the NLTP Library.

Pairs directly with the Arduino IDE sketches in `arduino/NLTP/examples/`:
  - Mode `send`: pushes a file or test payload to `ESP32_Encrypted_Receiver.ino`
  - Mode `listen`: runs a shore server on UDP 9700 to receive periodic batches from
    `ESP32_Marine_Buoy_Sender.ino`

Usage:
    # Push a configuration or firmware file to an ESP32 on Wi-Fi:
    python examples/python/04_talk_to_arduino_esp32.py send --esp-ip 192.168.1.50 --port 9700 --psk "my-marine-secret-2026"

    # Listen on laptop/shore PC to receive batches uploaded by ESP32_Marine_Buoy_Sender.ino:
    python examples/python/04_talk_to_arduino_esp32.py listen --port 9700 --psk "my-marine-secret-2026"
"""
from __future__ import annotations

import argparse
import os
import struct
import tempfile
from pathlib import Path

from nltp import NLTPClient, NLTPServer


def run_send(esp_ip: str, port: int, psk: str, file_path: str | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        if file_path:
            target = Path(file_path)
        else:
            target = Path(tmp) / "buoy_config_16kb.bin"
            target.write_bytes(os.urandom(16 * 1024))

        print(f"Sending {target.name} ({target.stat().st_size:,} B) -> {esp_ip}:{port} (encrypted)...")
        with NLTPClient((esp_ip, port), psk=psk, max_tx_kbps=10_000) as cli:
            receipt = cli.send_file(target, timeout=30.0)
        print(
            f"Done: verified_sha256={receipt['verified_sha256']} | "
            f"elapsed={receipt['elapsed_s']:.3f}s | goodput={receipt['goodput_kbps']:.1f} kbps"
        )


def run_listen(bind_host: str, port: int, psk: str, save_dir: str) -> None:
    out_dir = Path(save_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Listening for Arduino/ESP32 uploads on {bind_host}:{port} (saving to {out_dir.resolve()})...")
    print("Press Ctrl+C to stop.")
    with NLTPServer((bind_host, port), save_dir=out_dir, psk=psk) as srv:
        try:
            while True:
                item = srv.recv(timeout=2.0)
                if item is None:
                    continue
                print(
                    f"[RX from {item.peer_addr[0]}:{item.peer_addr[1]}] "
                    f"file={item.filename!r} bytes={len(item.data):,} sha256_ok={item.sha256_verified}"
                )
                # Decode sample 16-byte buoy telemetry record if sent by ESP32_Marine_Buoy_Sender.ino
                if item.filename.startswith("buoy_batch") and len(item.data) >= 16 and len(item.data) % 16 == 0:
                    seq, uptime_ms, temp_c_x100, wave_cm, batt_mv, rssi_dbm = struct.unpack_from("<IIhhHh", item.data, 0)
                    print(
                        f"   First sample -> seq={seq} uptime={uptime_ms}ms "
                        f"temp={temp_c_x100/100:.2f}C wave={wave_cm}cm batt={batt_mv/1000:.2f}V rssi={rssi_dbm}dBm"
                    )
        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Send files to or receive batches from an Arduino/ESP32 NLTP node.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="Send a file to ESP32_Encrypted_Receiver.ino")
    p_send.add_argument("--esp-ip", required=True, help="ESP32 IP address printed on Serial Monitor")
    p_send.add_argument("--port", type=int, default=9700)
    p_send.add_argument("--psk", default="my-marine-secret-2026", help="Shared key or passphrase")
    p_send.add_argument("--file", default=None, help="File to send (defaults to a generated 16 KB binary)")

    p_listen = sub.add_parser("listen", help="Receive periodic uploads from ESP32_Marine_Buoy_Sender.ino")
    p_listen.add_argument("--bind", default="0.0.0.0")
    p_listen.add_argument("--port", type=int, default=9700)
    p_listen.add_argument("--psk", default="my-marine-secret-2026", help="Shared key or passphrase")
    p_listen.add_argument("--save-dir", default="./esp32_inbox")

    args = ap.parse_args()
    if args.cmd == "send":
        run_send(args.esp_ip, args.port, args.psk, args.file)
    else:
        run_listen(args.bind, args.port, args.psk, args.save_dir)


if __name__ == "__main__":
    main()
