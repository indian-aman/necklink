"""NeckCord (`neckcord` v1.0.0): Descending Cervical Bottleneck & Self-Healing VNC Motor/Thruster Cord."""

from .allocator import AllocationResult, ShuntingThrusterCord
from .cervical import (
    CervicalBottleneckLink,
    CervicalCRCError,
    DescendingFrame,
    GaitMode,
    compute_crc4,
    decode_descending_4b,
    encode_descending_4b,
)
from .cpg import CPGState, LIMB_NAMES, SegmentalCPGCord, compute_support_margin

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "AllocationResult",
    "CPGState",
    "CervicalBottleneckLink",
    "CervicalCRCError",
    "DescendingFrame",
    "GaitMode",
    "LIMB_NAMES",
    "SegmentalCPGCord",
    "ShuntingThrusterCord",
    "compute_crc4",
    "compute_support_margin",
    "decode_descending_4b",
    "encode_descending_4b",
]
