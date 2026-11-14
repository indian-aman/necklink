"""NeckCord (`neckcord` v3.0.0): BANC v888 VNC Premotor CPG, Cervical Bottleneck & Engineering Actuators."""

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
from .cpg import (
    CPGState,
    LIMB_NAMES,
    SegmentalCPGCord,
    compute_support_margin,
    compute_tripod_order,
    load_banc_vnc_weights,
    maslov_sneppen_shuffle_vnc,
)
from .esc_observer import ESCFaultCode, ESCObserver, ESCObserverResult
from .ik import HexapodLegIK, LegIKResult

__version__ = "3.0.0"

__all__ = [
    "__version__",
    "AllocationResult",
    "CPGState",
    "CervicalBottleneckLink",
    "CervicalCRCError",
    "DescendingFrame",
    "ESCFaultCode",
    "ESCObserver",
    "ESCObserverResult",
    "GaitMode",
    "HexapodLegIK",
    "LIMB_NAMES",
    "LegIKResult",
    "SegmentalCPGCord",
    "ShuntingThrusterCord",
    "compute_crc4",
    "compute_support_margin",
    "compute_tripod_order",
    "decode_descending_4b",
    "encode_descending_4b",
    "load_banc_vnc_weights",
    "maslov_sneppen_shuffle_vnc",
]
