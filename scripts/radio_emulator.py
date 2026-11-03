"""
Radio-link emulator: a UDP relay that sits between this laptop and a device (e.g. the ESP32) and applies
radio-like impairments in each direction — random loss, bursty fades (Gilbert-Elliott), delay, jitter
(which also reorders packets) and a bandwidth limit.

    laptop NLTP client --> 127.0.0.1:<listen_port> --[emulated radio]--> device:<port>
                       <--                         <--                <--

The device sees the emulator's address as its peer, so replies come back through it as well. UDP only:
TCP cannot be impaired this way (a relay would terminate the TCP connection instead of dropping segments).

Standalone:  python scripts/radio_emulator.py --device 192.168.1.50:9700 --profile marine-radio
"""
from __future__ import annotations

import argparse
import heapq
import random
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Address = Tuple[str, int]


@dataclass
class RadioProfile:
    name: str
    loss_pct: float          # random loss in the "good" state
    burst_pct: float         # long-run share of time spent in a fade (bad state), 0 = no fades
    burst_len: float         # mean fade length, in packets
    delay_ms: float          # one-way base delay
    jitter_ms: float         # one-way delay standard deviation
    rate_kbps: float         # link bandwidth


PROFILES: Dict[str, RadioProfile] = {
    "wifi-far": RadioProfile("wifi-far", 5.0, 0.0, 0.0, 10.0, 5.0, 5000.0),
    "4g-weak": RadioProfile("4g-weak", 3.0, 4.0, 6.0, 60.0, 20.0, 2000.0),
    "marine-radio": RadioProfile("marine-radio", 5.0, 6.0, 8.0, 150.0, 30.0, 1000.0),
    "very-bad": RadioProfile("very-bad", 20.0, 0.0, 0.0, 250.0, 40.0, 1000.0),
}


class _Direction:
    """One direction of the link: loss model, serialization at rate_kbps, delay + jitter."""

    def __init__(self, p: RadioProfile, rng: random.Random) -> None:
        self.p = p
        self.rng = rng
        self.bad = False
        self.link_free_at = 0.0
        self.sent = self.dropped = 0

    def schedule(self, size: int, now: float) -> Optional[float]:
        self.sent += 1
        p = self.p
        if p.burst_pct > 0:
            # Gilbert-Elliott: leave a fade with prob 1/burst_len; enter one so that it covers burst_pct of time
            leave = 1.0 / max(1.5, p.burst_len)
            pi_b = p.burst_pct / 100.0
            enter = pi_b * leave / max(1e-6, 1.0 - pi_b)
            if self.bad:
                self.bad = self.rng.random() >= leave
            else:
                self.bad = self.rng.random() < enter
        loss = 0.92 if self.bad else p.loss_pct / 100.0
        tx_time = size * 8.0 / (p.rate_kbps * 1000.0)
        start = max(now, self.link_free_at)
        self.link_free_at = start + tx_time      # bandwidth is used even if the packet is then lost
        if self.rng.random() < loss:
            self.dropped += 1
            return None
        delay = max(0.0, self.rng.gauss(p.delay_ms, p.jitter_ms)) / 1000.0
        return start + tx_time + delay


class RadioEmulator:
    def __init__(self, device: Address, profile: RadioProfile, listen_port: int = 9750, seed: int = 7) -> None:
        self.device = device
        self.profile = profile
        rng = random.Random(seed)
        self.up = _Direction(profile, rng)     # laptop -> device
        self.down = _Direction(profile, rng)   # device -> laptop
        self.local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.local.bind(("127.0.0.1", listen_port))
        self.remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.remote.bind(("0.0.0.0", 0))
        for s in (self.local, self.remote):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            s.settimeout(0.002)
        self.client: Optional[Address] = None
        self.queue: List[Tuple[float, int, bool, bytes]] = []
        self.seq = 0
        self.lock = threading.Lock()
        self.running = True
        self.threads = [threading.Thread(target=self._rx, args=(self.local, True), daemon=True),
                        threading.Thread(target=self._rx, args=(self.remote, False), daemon=True),
                        threading.Thread(target=self._tx, daemon=True)]
        for t in self.threads:
            t.start()

    @property
    def address(self) -> Address:
        return self.local.getsockname()

    def _rx(self, sock: socket.socket, upstream: bool) -> None:
        while self.running:
            try:
                data, addr = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            if upstream:
                self.client = addr
            now = time.perf_counter()
            with self.lock:
                due = (self.up if upstream else self.down).schedule(len(data) + 28, now)
                if due is not None:
                    self.seq += 1
                    heapq.heappush(self.queue, (due, self.seq, upstream, data))

    def _tx(self) -> None:
        while self.running:
            now = time.perf_counter()
            out = []
            with self.lock:
                while self.queue and self.queue[0][0] <= now:
                    out.append(heapq.heappop(self.queue))
            for _, _, upstream, data in out:
                try:
                    if upstream:
                        self.remote.sendto(data, self.device)
                    elif self.client:
                        self.local.sendto(data, self.client)
                except OSError:
                    pass
            time.sleep(0.0005)

    def stats(self) -> Dict[str, float]:
        return {"up_sent": self.up.sent, "up_dropped": self.up.dropped,
                "down_sent": self.down.sent, "down_dropped": self.down.dropped,
                "up_loss_pct": round(100.0 * self.up.dropped / max(1, self.up.sent), 1),
                "down_loss_pct": round(100.0 * self.down.dropped / max(1, self.down.sent), 1)}

    def close(self) -> None:
        self.running = False
        for t in self.threads:
            t.join(timeout=1.0)
        self.local.close()
        self.remote.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, help="ip:port of the device behind the emulated radio")
    ap.add_argument("--profile", default="marine-radio", choices=sorted(PROFILES))
    ap.add_argument("--listen-port", type=int, default=9750)
    a = ap.parse_args()
    host, port = a.device.rsplit(":", 1)
    emu = RadioEmulator((host, int(port)), PROFILES[a.profile], a.listen_port)
    print(f"emulating {a.profile} between 127.0.0.1:{a.listen_port} and {a.device} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(5)
            print(emu.stats())
    except KeyboardInterrupt:
        emu.close()


if __name__ == "__main__":
    main()
