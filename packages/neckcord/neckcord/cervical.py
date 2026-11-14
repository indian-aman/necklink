"""4-byte Descending Cervical Command Frame codec (`32 bits` = `400 B/s` at `100 Hz`).

Models the *Drosophila* cervical connective bottleneck where descending neurons
(`DNa02` asymmetric steering, `DNp09` forward drive, `MDN` reverse locomotion,
and `Giant Fiber` emergency freeze) compress full-body behavioral intent into a
compact stream decoded autonomously by the Ventral Nerve Cord (`VNC`).

Wire layout (`32 bits`, big-endian `uint32`):
  - Bits `[31:24]` (`8 bits`): `drive_fwd` signed symmetric `-127..+127` -> `[-1.0, +1.0]`
  - Bits `[23:16]` (`8 bits`): `turn_yaw`  signed symmetric `-127..+127` -> `[-1.0, +1.0]`
  - Bits `[15:9]`  (`7 bits`): `strafe_lat` signed symmetric `-63..+63`  -> `[-1.0, +1.0]`
  - Bits `[8:7]`   (`2 bits`): `gait_mode` unsigned `0..3` (`AUTO`, `TRIPOD`, `TETRAPOD`, `WAVE`)
  - Bit  `[6]`     (`1 bit`) : `freeze_reflex` (`0` = normal, `1` = Giant Fiber freeze)
  - Bits `[5:4]`   (`2 bits`): `seq` rolling 2-bit frame counter `0..3`
  - Bits `[3:0]`   (`4 bits`): `crc4` (`x^4 + x + 1`, non-zero seed + final XOR) over bits `[31:4]`
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
import numpy as np


class GaitMode(IntEnum):
    """Descending gait selection code (`2 bits`, `0..3`)."""

    AUTO = 0
    TRIPOD = 1
    TETRAPOD = 2
    WAVE = 3


class CervicalCRCError(ValueError):
    """Raised when a 4-byte cervical frame fails length, range, or CRC-4 validation."""


def compute_crc4(upper28: int) -> int:
    """Compute 4-bit CRC (`x^4 + x + 1`, poly `0x3`, init `0x9`, xorout `0x5`) over 28 bits.

    Chosen seed (`0x9`) and final XOR (`0x5`) guarantee that both stuck-low (`0x00000000`)
    and stuck-high (`0xFFFFFFFF`) bus faults fail CRC validation, in addition to 100% of
    single-bit flips across all 32 bits. Like any 4-bit check it lets roughly 1 in 16 random
    multi-bit corruptions through (measured: ~4% of 2-bit errors, ~6% of random words), so
    run the frame over a link with its own integrity check (e.g. NLTP's AEAD or a CAN CRC).
    """
    crc = 0x9
    val = int(upper28) & 0x0FFFFFFF
    for shift in range(27, -1, -1):
        fb = ((val >> shift) ^ (crc >> 3)) & 0x1
        crc = ((crc << 1) & 0xF) ^ (0x3 if fb else 0x0)
    return (crc ^ 0x5) & 0xF


def _quantize_sym(val: float, max_int: int) -> int:
    clipped = max(-1.0, min(1.0, float(val)))
    if clipped >= 0.0:
        return int(clipped * max_int + 0.5)
    return -int((-clipped) * max_int + 0.5)


@dataclass
class DescendingFrame:
    """Compact brain-to-VNC descending motor intent (`4 bytes` on the wire)."""

    drive_fwd: float = 0.0
    turn_yaw: float = 0.0
    strafe_lat: float = 0.0
    gait_mode: int = int(GaitMode.AUTO)
    freeze_reflex: bool = False
    seq: int = 0

    def __post_init__(self) -> None:
        self.drive_fwd = float(max(-1.0, min(1.0, self.drive_fwd)))
        self.turn_yaw = float(max(-1.0, min(1.0, self.turn_yaw)))
        self.strafe_lat = float(max(-1.0, min(1.0, self.strafe_lat)))
        gait = int(self.gait_mode)
        if not 0 <= gait <= 3:
            raise ValueError(f"gait_mode must be in 0..3, got {self.gait_mode!r}")
        self.gait_mode = gait
        self.freeze_reflex = bool(self.freeze_reflex)
        self.seq = int(self.seq) & 0x3

    def to_bytes(self, seq: int | None = None) -> bytes:
        """Pack this descending frame into 4 wire bytes."""
        return encode_descending_4b(self, seq=seq)

    @classmethod
    def from_bytes(
        cls,
        raw: bytes | bytearray | memoryview,
        expected_seq: int | None = None,
    ) -> "DescendingFrame":
        """Unpack and CRC-verify a 4-byte cervical command frame."""
        return decode_descending_4b(raw, expected_seq=expected_seq)

    def to_wrench(
        self,
        max_surge: float = 60.0,
        max_sway: float = 40.0,
        max_yaw: float = 25.0,
    ) -> np.ndarray:
        """Map descending intent to 3-DOF body wrench `[Fx, Fy, Tz]` (`0` when frozen)."""
        if self.freeze_reflex:
            return np.zeros(3, dtype=np.float64)
        return np.array(
            [
                self.drive_fwd * float(max_surge),
                self.strafe_lat * float(max_sway),
                self.turn_yaw * float(max_yaw),
            ],
            dtype=np.float64,
        )

    def to_wrench_4d(
        self,
        hover_thrust: float = 36.0,
        max_roll: float = 4.0,
        max_pitch: float = 4.0,
        max_yaw: float = 2.0,
    ) -> np.ndarray:
        """Map descending intent to 4-DOF aerial wrench `[Fz, Tx, Ty, Tz]` (`0` when frozen)."""
        if self.freeze_reflex:
            return np.zeros(4, dtype=np.float64)
        return np.array(
            [
                float(hover_thrust),
                -self.strafe_lat * float(max_roll),
                self.drive_fwd * float(max_pitch),
                self.turn_yaw * float(max_yaw),
            ],
            dtype=np.float64,
        )


def encode_descending_4b(
    frame: DescendingFrame | None = None,
    *,
    drive_fwd: float = 0.0,
    turn_yaw: float = 0.0,
    strafe_lat: float = 0.0,
    gait_mode: int = 0,
    freeze_reflex: bool = False,
    seq: int | None = None,
) -> bytes:
    """Pack a `DescendingFrame` (or keyword arguments) into an exact 4-byte wire frame."""
    if frame is None:
        frame = DescendingFrame(
            drive_fwd=drive_fwd,
            turn_yaw=turn_yaw,
            strafe_lat=strafe_lat,
            gait_mode=gait_mode,
            freeze_reflex=freeze_reflex,
            seq=0 if seq is None else seq,
        )
    eff_seq = (frame.seq if seq is None else int(seq)) & 0x3
    gait = int(frame.gait_mode)
    if not 0 <= gait <= 3:
        raise ValueError(f"gait_mode must be in 0..3, got {gait}")

    q_fwd = _quantize_sym(frame.drive_fwd, 127)
    q_yaw = _quantize_sym(frame.turn_yaw, 127)
    q_lat = _quantize_sym(frame.strafe_lat, 63)
    freeze_bit = 1 if frame.freeze_reflex else 0

    u_fwd = q_fwd & 0xFF
    u_yaw = q_yaw & 0xFF
    u_lat = q_lat & 0x7F

    upper28 = (
        (u_fwd << 20)
        | (u_yaw << 12)
        | (u_lat << 5)
        | ((gait & 0x3) << 3)
        | (freeze_bit << 2)
        | eff_seq
    )
    crc = compute_crc4(upper28)
    word32 = ((upper28 << 4) | crc) & 0xFFFFFFFF
    return struct.pack(">I", word32)


def decode_descending_4b(
    raw: bytes | bytearray | memoryview,
    *,
    expected_seq: int | None = None,
) -> DescendingFrame:
    """Decode and validate a 4-byte cervical command frame.

    Raises `CervicalCRCError` if the frame is not 4 bytes, fails CRC-4 verification,
    contains reserved out-of-range symmetric codes (`-128` / `-64`), or mismatches
    `expected_seq` when provided.
    """
    buf = memoryview(raw)
    if len(buf) != 4:
        raise CervicalCRCError(f"Expected 4-byte cervical frame, got {len(buf)} bytes")

    (word32,) = struct.unpack(">I", buf)
    upper28 = (word32 >> 4) & 0x0FFFFFFF
    rx_crc = word32 & 0xF
    calc_crc = compute_crc4(upper28)
    if rx_crc != calc_crc:
        raise CervicalCRCError(
            f"Cervical CRC-4 mismatch: received 0x{rx_crc:X}, expected 0x{calc_crc:X}"
        )

    u_fwd = (upper28 >> 20) & 0xFF
    u_yaw = (upper28 >> 12) & 0xFF
    u_lat = (upper28 >> 5) & 0x7F
    gait = (upper28 >> 3) & 0x3
    freeze = bool((upper28 >> 2) & 0x1)
    seq = upper28 & 0x3

    q_fwd = u_fwd - 256 if u_fwd >= 128 else u_fwd
    q_yaw = u_yaw - 256 if u_yaw >= 128 else u_yaw
    q_lat = u_lat - 128 if u_lat >= 64 else u_lat

    if q_fwd == -128 or q_yaw == -128 or q_lat == -64:
        raise CervicalCRCError("Reserved symmetric saturation code in cervical frame")

    if expected_seq is not None and seq != (int(expected_seq) & 0x3):
        raise CervicalCRCError(
            f"Sequence mismatch: received seq={seq}, expected {int(expected_seq) & 0x3}"
        )

    return DescendingFrame(
        drive_fwd=q_fwd / 127.0,
        turn_yaw=q_yaw / 127.0,
        strafe_lat=q_lat / 63.0,
        gait_mode=gait,
        freeze_reflex=freeze,
        seq=seq,
    )


class CervicalBottleneckLink:
    """Stateful rolling-sequence wrapper for 4-byte cervical TX/RX streams (`400 B/s` @ `100 Hz`).

    The sequence counter is 2 bits, so ``dropped_frames`` counts gaps modulo 4: a burst of
    exactly 4 (or 8, ...) lost frames is invisible. Pair it with a receive-timeout watchdog.
    """

    def __init__(self) -> None:
        self._tx_seq: int = 0
        self._rx_seq: int | None = None
        self.tx_frames: int = 0
        self.rx_frames: int = 0
        self.dropped_frames: int = 0
        self.crc_errors: int = 0

    def pack(self, frame: DescendingFrame) -> bytes:
        """Encode `frame` with the next 2-bit rolling sequence number."""
        seq = self._tx_seq
        self._tx_seq = (self._tx_seq + 1) & 0x3
        self.tx_frames += 1
        return encode_descending_4b(frame, seq=seq)  # does not modify the caller's frame

    def unpack(self, raw: bytes | bytearray | memoryview) -> DescendingFrame:
        """Decode `raw`, tracking CRC failures and rolling sequence gaps."""
        try:
            frame = decode_descending_4b(raw)
        except CervicalCRCError:
            self.crc_errors += 1
            raise
        if self._rx_seq is not None:
            expected = (self._rx_seq + 1) & 0x3
            gap = (frame.seq - expected) & 0x3
            if gap != 0:
                self.dropped_frames += gap
        self._rx_seq = frame.seq
        self.rx_frames += 1
        return frame

    @staticmethod
    def wire_bandwidth_bps(rate_hz: float = 100.0) -> float:
        """Return payload byte rate in bytes/s (`400.0 B/s` at `100 Hz`)."""
        return 4.0 * float(rate_hz)
