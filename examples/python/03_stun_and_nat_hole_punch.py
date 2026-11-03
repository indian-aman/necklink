"""
Example 3: Live RFC 5389 STUN Public Endpoint Discovery & Direct P2P NAT Hole-Punching.

Demonstrates:
  1. Discovering your live public WAN IP, UDP port, RTT, and NAT Cone/Symmetric behavior
     using public STUN servers (`stun.cloudflare.com:3478`, `stun.l.google.com:19302`).
  2. Punching a direct encrypted UDP pinhole between two endpoints without a relay.

Run:
    python examples/python/03_stun_and_nat_hole_punch.py
"""
from __future__ import annotations

from nltp import NLTPClient, NLTPServer, discover_wan_endpoint, generate_nltp_key


def main() -> None:
    # Part A: Live public Internet STUN probe (skips gracefully if offline)
    try:
        wan = discover_wan_endpoint(timeout_s=2.5)
        # Mask public IP in console logs by default while showing NAT summary & RTT
        masked_ip = ".".join(["xxx", "xxx", "xxx", wan.public_ip.split(".")[-1]]) if "." in wan.public_ip else "masked"
        print(
            f"[Live STUN] Public endpoint: {masked_ip}:{wan.public_port} | "
            f"NAT: {wan.nat_summary} | RTT: {wan.rtt_ms:.1f} ms"
        )
    except Exception as exc:
        print(f"[Live STUN] Offline or UDP 3478 blocked ({exc}); continuing local P2P demo.")

    # Part B: Direct peer-to-peer hole-punch handshake + encrypted message
    key = generate_nltp_key()
    with NLTPServer(("127.0.0.1", 0), psk=key, keepalive_s=10.0) as side_a:
        with NLTPClient(("127.0.0.1", 9), psk=key, keepalive_s=10.0) as side_b:
            # In real Internet use, exchange (wan.public_ip, wan.public_port) from side_a.discover_wan()
            # and side_b.discover_wan() via your signaling API/MQTT/SMS, then punch both sides:
            addr_a = side_a.getsockname()
            addr_b = side_b.getsockname()

            punched_a = side_a.punch_p2p(addr_b, timeout_s=0.6)
            punched_b = side_b.punch_p2p(addr_a, timeout_s=0.8)
            side_b.set_peer_endpoint(addr_a)

            receipt = side_b.send_message("Direct P2P link active with 10s NAT keepalives!")
            msg = side_a.recv(timeout=3.0)
            assert msg is not None
            print(
                f"[P2P] Hole-punch={punched_a or punched_b} | "
                f"Verified={receipt['verified_sha256']} | Text={msg.as_text()!r}"
            )


if __name__ == "__main__":
    main()
