"""
Zero-Dependency Official MAVLink v2 Wire Codec & UDP Bridge (`necklink_cns.mavlink`).

Byte-exact Python twin of `csrc/nl_mavlink.c` supporting official `common.xml`
MAVLink v2 framing (`STX = 0xFD`, X.25 CRC-16/MCRF4XX, `CRC_EXTRA`, and MAVLink v2
trailing-zero payload trimming) for plug-and-play connection to ArduPilot, PX4,
MAVProxy, and QGroundControl over UDP (e.g., `127.0.0.1:14550`) or UART.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

MAV2_STX = 0xFD
MAV2_HEADER_LEN = 10

MSG_HEARTBEAT = 0
MSG_SCALED_IMU = 26
MSG_MANUAL_CONTROL = 69
MSG_STATUSTEXT = 253
MSG_OBSTACLE_DISTANCE = 330

CRC_EXTRA_MAP = {
    MSG_HEARTBEAT: 50,
    MSG_SCALED_IMU: 170,
    MSG_MANUAL_CONTROL: 243,
    MSG_STATUSTEXT: 83,
    MSG_OBSTACLE_DISTANCE: 23,
}

MAV_TYPE_ONBOARD_CONTROLLER = 18
MAV_AUTOPILOT_INVALID = 8
MAV_STATE_ACTIVE = 4
MAV_SEVERITY_CRITICAL = 2
MAV_SEVERITY_WARNING = 4
MAV_DISTANCE_SENSOR_OPTICAL = 2


def mav_crc_accumulate(byte_val: int, crc: int = 0xFFFF) -> int:
    """Official X.25 CRC-16/MCRF4XX single-byte accumulator."""
    tmp = (byte_val ^ (crc & 0xFF)) & 0xFF
    tmp = (tmp ^ ((tmp << 4) & 0xFF)) & 0xFF
    return (((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF)


def mav_crc_buffer(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc = mav_crc_accumulate(b, crc)
    return crc


def pack_mavlink2_raw(
    sysid: int,
    compid: int,
    seq: int,
    msgid: int,
    payload: bytes,
    crc_extra: Optional[int] = None,
) -> bytes:
    """Encode a wire-valid MAVLink v2 packet with trailing-zero payload trimming."""
    if crc_extra is None:
        if msgid not in CRC_EXTRA_MAP:
            raise ValueError(f"Unknown CRC_EXTRA for msgid={msgid}")
        crc_extra = CRC_EXTRA_MAP[msgid]

    trimmed = payload
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed = trimmed[:-1]

    hdr_no_stx = bytes(
        [
            len(trimmed) & 0xFF,
            0,  # incompat_flags
            0,  # compat_flags
            seq & 0xFF,
            sysid & 0xFF,
            compid & 0xFF,
            msgid & 0xFF,
            (msgid >> 8) & 0xFF,
            (msgid >> 16) & 0xFF,
        ]
    )
    crc = mav_crc_buffer(hdr_no_stx, 0xFFFF)
    crc = mav_crc_buffer(trimmed, crc)
    crc = mav_crc_accumulate(crc_extra & 0xFF, crc)
    return bytes([MAV2_STX]) + hdr_no_stx + trimmed + struct.pack("<H", crc)


@dataclass
class MavHeartbeat:
    custom_mode: int = 0
    type: int = MAV_TYPE_ONBOARD_CONTROLLER
    autopilot: int = MAV_AUTOPILOT_INVALID
    base_mode: int = 0
    system_status: int = MAV_STATE_ACTIVE
    mavlink_version: int = 3

    def to_payload(self) -> bytes:
        ver = 3 if self.mavlink_version == 0 else self.mavlink_version
        return struct.pack(
            "<IBBBBB",
            self.custom_mode & 0xFFFFFFFF,
            self.type & 0xFF,
            self.autopilot & 0xFF,
            self.base_mode & 0xFF,
            self.system_status & 0xFF,
            ver & 0xFF,
        )

    @classmethod
    def from_payload(cls, raw: bytes) -> "MavHeartbeat":
        padded = raw.ljust(9, b"\x00")[:9]
        cm, tp, ap, bm, ss, mv = struct.unpack("<IBBBBB", padded)
        return cls(cm, tp, ap, bm, ss, mv)


@dataclass
class MavScaledImu:
    time_boot_ms: int
    xacc_mg: int
    yacc_mg: int
    zacc_mg: int
    xgyro_mrad_s: int
    ygyro_mrad_s: int
    zgyro_mrad_s: int
    xmag_mgauss: int
    ymag_mgauss: int
    zmag_mgauss: int

    def to_payload(self) -> bytes:
        return struct.pack(
            "<Ihhhhhhhhh",
            self.time_boot_ms & 0xFFFFFFFF,
            int(self.xacc_mg),
            int(self.yacc_mg),
            int(self.zacc_mg),
            int(self.xgyro_mrad_s),
            int(self.ygyro_mrad_s),
            int(self.zgyro_mrad_s),
            int(self.xmag_mgauss),
            int(self.ymag_mgauss),
            int(self.zmag_mgauss),
        )

    @classmethod
    def from_payload(cls, raw: bytes) -> "MavScaledImu":
        padded = raw.ljust(22, b"\x00")[:22]
        return cls(*struct.unpack("<Ihhhhhhhhh", padded))


@dataclass
class MavManualControl:
    x: int  # pitch / surge [-1000..1000]
    y: int  # roll / sway   [-1000..1000]
    z: int  # thrust        [0..1000] or [-1000..1000]
    r: int  # yaw           [-1000..1000]
    buttons: int = 0
    target: int = 1

    def to_payload(self) -> bytes:
        return struct.pack(
            "<hhhhHB",
            int(max(-1000, min(1000, self.x))),
            int(max(-1000, min(1000, self.y))),
            int(max(-1000, min(1000, self.z))),
            int(max(-1000, min(1000, self.r))),
            self.buttons & 0xFFFF,
            self.target & 0xFF,
        )

    @classmethod
    def from_payload(cls, raw: bytes) -> "MavManualControl":
        padded = raw.ljust(11, b"\x00")[:11]
        return cls(*struct.unpack("<hhhhHB", padded))


@dataclass
class MavStatusText:
    severity: int
    text: str

    def to_payload(self) -> bytes:
        txt_bytes = self.text.encode("ascii", errors="replace")[:50].ljust(50, b"\x00")
        return struct.pack("<B", self.severity & 0xFF) + txt_bytes

    @classmethod
    def from_payload(cls, raw: bytes) -> "MavStatusText":
        padded = raw.ljust(51, b"\x00")[:51]
        sev = padded[0]
        txt = padded[1:51].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        return cls(sev, txt)


@dataclass
class MavObstacleDistance:
    time_usec: int
    distances_cm: Tuple[int, ...]  # length 72
    min_distance_cm: int = 20
    max_distance_cm: int = 1500
    sensor_type: int = MAV_DISTANCE_SENSOR_OPTICAL
    increment_deg: int = 5

    def to_payload(self) -> bytes:
        dists = list(self.distances_cm[:72]) + [0xFFFF] * max(0, 72 - len(self.distances_cm))
        return struct.pack(
            "<Q72HHHBB",
            self.time_usec & 0xFFFFFFFFFFFFFFFF,
            *[d & 0xFFFF for d in dists],
            self.min_distance_cm & 0xFFFF,
            self.max_distance_cm & 0xFFFF,
            self.sensor_type & 0xFF,
            self.increment_deg & 0xFF,
        )

    @classmethod
    def from_payload(cls, raw: bytes) -> "MavObstacleDistance":
        padded = raw.ljust(158, b"\x00")[:158]
        vals = struct.unpack("<Q72HHHBB", padded)
        return cls(
            time_usec=vals[0],
            distances_cm=tuple(vals[1:73]),
            min_distance_cm=vals[73],
            max_distance_cm=vals[74],
            sensor_type=vals[75],
            increment_deg=vals[76],
        )


@dataclass
class MavFrame:
    msgid: int
    sysid: int
    compid: int
    seq: int
    payload: bytes


class MavLink2Parser:
    """Streaming byte-by-byte MAVLink v2 parser matching `nl_mav_parser_t`."""

    PROVENANCE = "engineering"

    def __init__(self) -> None:
        self._buf = bytearray()
        self.rx_ok_count = 0
        self.rx_crc_errors = 0

    def feed(self, chunk: bytes) -> List[MavFrame]:
        self._buf.extend(chunk)
        frames: List[MavFrame] = []
        while len(self._buf) >= 12:
            if self._buf[0] != MAV2_STX:
                del self._buf[0]
                continue
            plen = self._buf[1]
            total = 10 + plen + 2
            if len(self._buf) < total:
                break
            pkt = bytes(self._buf[:total])
            hdr_no_stx = pkt[1:10]
            payload = pkt[10 : 10 + plen]
            rx_crc = struct.unpack("<H", pkt[10 + plen : total])[0]
            msgid = hdr_no_stx[6] | (hdr_no_stx[7] << 8) | (hdr_no_stx[8] << 16)
            extra = CRC_EXTRA_MAP.get(msgid)
            if extra is not None:
                calc = mav_crc_buffer(hdr_no_stx, 0xFFFF)
                calc = mav_crc_buffer(payload, calc)
                calc = mav_crc_accumulate(extra, calc)
                if calc == rx_crc:
                    self.rx_ok_count += 1
                    frames.append(
                        MavFrame(
                            msgid=msgid,
                            sysid=hdr_no_stx[4],
                            compid=hdr_no_stx[5],
                            seq=hdr_no_stx[3],
                            payload=payload,
                        )
                    )
                    del self._buf[:total]
                    continue
                self.rx_crc_errors += 1
            # Resync past false STX
            del self._buf[0]
        return frames


class MavLink2Codec:
    """Stateful sequence-numbered MAVLink v2 encoder + streaming decoder."""

    PROVENANCE = "engineering"

    def __init__(self, sysid: int = 42, compid: int = 191) -> None:
        self.sysid = sysid & 0xFF
        self.compid = compid & 0xFF
        self.seq = 0
        self.parser = MavLink2Parser()

    def _next_seq(self) -> int:
        s = self.seq
        self.seq = (self.seq + 1) & 0xFF
        return s

    def encode_heartbeat(self, msg: Optional[MavHeartbeat] = None) -> bytes:
        m = msg or MavHeartbeat()
        return pack_mavlink2_raw(self.sysid, self.compid, self._next_seq(), MSG_HEARTBEAT, m.to_payload())

    def encode_scaled_imu(self, msg: MavScaledImu) -> bytes:
        return pack_mavlink2_raw(self.sysid, self.compid, self._next_seq(), MSG_SCALED_IMU, msg.to_payload())

    def encode_manual_control(self, msg: MavManualControl) -> bytes:
        return pack_mavlink2_raw(self.sysid, self.compid, self._next_seq(), MSG_MANUAL_CONTROL, msg.to_payload())

    def encode_statustext(self, text: str, severity: int = MAV_SEVERITY_WARNING) -> bytes:
        m = MavStatusText(severity=severity, text=text)
        return pack_mavlink2_raw(self.sysid, self.compid, self._next_seq(), MSG_STATUSTEXT, m.to_payload())

    def encode_obstacle_distance(self, msg: MavObstacleDistance) -> bytes:
        return pack_mavlink2_raw(self.sysid, self.compid, self._next_seq(), MSG_OBSTACLE_DISTANCE, msg.to_payload())

    def feed(self, data: bytes) -> List[MavFrame]:
        return self.parser.feed(data)


class MavLinkUdpBridge:
    """Lightweight non-blocking UDP bridge for ArduPilot SITL / PX4 / MAVProxy."""

    PROVENANCE = "engineering"

    def __init__(
        self,
        bind_addr: Tuple[str, int] = ("127.0.0.1", 0),
        peer_addr: Optional[Tuple[str, int]] = None,
        sysid: int = 42,
        compid: int = 191,
    ) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind_addr)
        self.sock.setblocking(False)
        self.peer_addr = peer_addr
        self.codec = MavLink2Codec(sysid=sysid, compid=compid)

    @property
    def local_addr(self) -> Tuple[str, int]:
        addr = self.sock.getsockname()
        return (str(addr[0]), int(addr[1]))

    def send_packet(self, pkt: bytes, addr: Optional[Tuple[str, int]] = None) -> int:
        dst = addr or self.peer_addr
        if dst is None:
            raise ValueError("No MAVLink peer_addr configured")
        return self.sock.sendto(pkt, dst)

    def poll_frames(self) -> List[MavFrame]:
        frames: List[MavFrame] = []
        while True:
            try:
                data, sender = self.sock.recvfrom(2048)
            except BlockingIOError:
                break
            if self.peer_addr is None:
                self.peer_addr = (str(sender[0]), int(sender[1]))
            frames.extend(self.codec.feed(data))
        return frames

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "MavLinkUdpBridge":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


MAVLink2Codec = MavLink2Codec
MAVLink2Parser = MavLink2Parser
