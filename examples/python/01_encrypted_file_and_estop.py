"""
Example 1: Encrypted File Transfer, JSON Messages, and Urgent ESTOP Reflex in Python.

Run from anywhere:
    python examples/python/01_encrypted_file_and_estop.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from nltp import NLTPClient, NLTPServer, generate_nltp_key


def main() -> None:
    # 1. Generate a 256-bit ChaCha20-Poly1305 shared key (save & share once between peers)
    key = generate_nltp_key()
    print(f"Generated 256-bit NLTP key: {key[:16]}...")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        inbox_dir = tmp_dir / "inbox"

        # Create a 64 KB binary firmware/log payload to send
        firmware_path = tmp_dir / "buoy_firmware_v1.2.bin"
        firmware_path.write_bytes(os.urandom(64 * 1024))

        # 2. Start an encrypted NLTP server on an OS-assigned UDP port (use e.g. 9000 in production)
        with NLTPServer(("127.0.0.1", 0), save_dir=inbox_dir, psk=key, loss_drop_sim=0.10) as srv:
            server_addr = srv.getsockname()
            print(f"NLTPServer listening on {server_addr[0]}:{server_addr[1]} (simulated 10% packet loss)")

            # 3. Connect an encrypted NLTP client in 0-RTT
            with NLTPClient(server_addr, psk=key, max_tx_kbps=20_000, loss_drop_sim=0.10) as cli:
                # A) Urgent reflex command (bypasses file queues with 0 head-of-line blocking)
                cli.send_reflex("ESTOP_SELF_CHECK_OK")
                reflex_item = srv.recv(timeout=2.0)
                assert reflex_item is not None and reflex_item.kind == "reflex"
                print(f"[Reflex]   Received instant command: {reflex_item.as_text()!r}")

                # B) Structured JSON message (verified end-to-end with SHA-256)
                cli.send_json({"station": "buoy-01", "battery_v": 12.74, "wave_hs_m": 1.85})
                json_item = srv.recv(timeout=5.0)
                assert json_item is not None and json_item.kind == "json"
                print(f"[JSON]     Received verified dict : {json_item.as_json()}")

                # C) Binary file / OTA firmware transfer over 10% bidirectional packet loss
                receipt = cli.send_file(firmware_path)
                file_item = srv.recv(timeout=5.0)
                assert receipt["verified_sha256"] is True
                assert file_item is not None and file_item.saved_path is not None
                assert file_item.saved_path.read_bytes() == firmware_path.read_bytes()

                print(
                    f"[File]     Delivered {receipt['filename']} ({receipt['bytes']:,} B) in "
                    f"{receipt['elapsed_s']:.3f}s | SHA-256 verified={receipt['verified_sha256']} | "
                    f"saved to {file_item.saved_path.name}"
                )


if __name__ == "__main__":
    main()
