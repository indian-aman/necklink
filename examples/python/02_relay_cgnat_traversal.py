"""
Example 2: Traversing Strict 4G/5G SIM Carrier CGNAT via Zero-Knowledge NLTPRelayHub.

When both endpoints sit behind strict mobile-carrier Symmetric CGNATs (no public IP,
no port forwarding), run `NLTPRelayHub` on any 1-port cloud VM. The relay holds ZERO keys
and sees only encrypted ChaCha20-Poly1305 ciphertext.

Run:
    python examples/python/02_relay_cgnat_traversal.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from nltp import NLTPClient, NLTPRelayHub, NLTPServer, generate_nltp_key


def main() -> None:
    shared_key = generate_nltp_key()
    room_name = "tridel-buoy-07"

    with tempfile.TemporaryDirectory() as tmp:
        inbox = Path(tmp) / "shore_inbox"
        payload_file = Path(tmp) / "telemetry_batch.bin"
        payload_file.write_bytes(os.urandom(32 * 1024))

        # 1. Public 1-port stateless relay (in production: `nltp relay --host 0.0.0.0 --port 9000`)
        with NLTPRelayHub(("127.0.0.1", 0)) as relay:
            relay_addr = relay.getsockname()
            print(f"Zero-knowledge NLTPRelayHub running at {relay_addr[0]}:{relay_addr[1]} (holds 0 keys)")

            # 2. Shore receiver behind Office NAT/Firewall registers in room `tridel-buoy-07`
            with NLTPServer(("127.0.0.1", 0), relay_addr=relay_addr, room=room_name, psk=shared_key, save_dir=inbox) as srv:
                # 3. Buoy client behind 4G SIM CGNAT connects to the same room
                with NLTPClient(relay_addr=relay_addr, room=room_name, psk=shared_key) as cli:
                    receipt = cli.send_file(payload_file)
                    rx = srv.recv(timeout=5.0)

                    assert receipt["verified_sha256"] is True
                    assert rx is not None and rx.data == payload_file.read_bytes()
                    print(
                        f"Relayed {receipt['bytes']:,} B across CGNAT room {room_name!r} in "
                        f"{receipt['elapsed_s']:.3f}s | E2E encrypted={receipt['encrypted']} | "
                        f"SHA-256={receipt['sha256'][:16]}..."
                    )


if __name__ == "__main__":
    main()
