"""
NeckLink CNS (`necklink_cns` v2.0.0) — Unified Adult Drosophila Whole-CNS Edge Autonomy Stack.
"""

from .cns import CNSOutputs, NeckLinkCNS
from .connectome import BANC_V888_PRIORS, ConnectomeArbitrator, DescendingState
from .mavlink import (
    MavFrame,
    MavHeartbeat,
    MavLink2Codec,
    MavLink2Parser,
    MavLinkUdpBridge,
    MavManualControl,
    MavObstacleDistance,
    MavScaledImu,
    MavStatusText,
)
from .ros2_node import NeckLinkROS2Adapter, TwistCommand, create_ros2_node

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "BANC_V888_PRIORS",
    "CNSOutputs",
    "ConnectomeArbitrator",
    "DescendingState",
    "MavFrame",
    "MavHeartbeat",
    "MavLink2Codec",
    "MavLink2Parser",
    "MavLinkUdpBridge",
    "MavManualControl",
    "MavObstacleDistance",
    "MavScaledImu",
    "MavStatusText",
    "NeckLinkCNS",
    "NeckLinkROS2Adapter",
    "TwistCommand",
    "create_ros2_node",
]
