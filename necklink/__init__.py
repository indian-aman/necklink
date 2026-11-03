"""
NeckLink: Connectome-Inspired Zero-Retransmission Data Transfer Protocol & Codec.

Translates three architectural principles discovered in the adult Drosophila
brain-to-body neck connective (325,354 neurons across MaleCNS v1.0 & Female BANC v888)
into a universal ultra-low-bandwidth, packet-loss-healing wire protocol:
  1. Contractive Twin-Mirror Efference Predictor (0-LSB bit-exact integer state sync)
  2. Epistatic Cryptic-Backup Echo & Subspace Projection (cliff-free loss recovery)
  3. Dale's-Law Push-Pull + Disinhibitory Shunting Gain Rescue (0-ACK variance/DC healing)
"""

from .connectome_priors import (
    DROSOPHILA_NECK_PRIORS,
    build_epistatic_cryptic_matrix_q8,
    measure_empirical_connectome_neck_stats,
)
from .codec import (
    NeckLinkConfig,
    NeckLinkEncoder,
    NeckLinkDecoder,
    NeckLinkSplitBottleneck,
    c_div,
)
from .baselines import (
    RawFloat32UDPCodec,
    Int8ZlibBlockCodec,
    SendOnDeltaUDPCodec,
    ADPCMLeakyUDPCodec,
    ReedSolomonGF256Codec,
    TCPARQAnalyticalEvaluator,
    simulate_packet_drops,
    compute_link_economics,
)
from .c_export import export_necklink_c99_header
from .internet import (
    AEADCipherContext,
    NLTPRelayHub,
    ReplayWindow64,
    STUNDiscoveryResult,
    TokenBucketPacer,
    discover_wan_endpoint,
    generate_nltp_key,
)
from .nltp import NLTPClient, NLTPEndpoint, NLTPServer, ReceivedItem, pack_nltp_frame, unpack_nltp_frame
from .socket_api import AsyncNeckSocket, NeckSocket, NeckUDPServer

try:
    from .quic_adapter import (
        LossyUDPRelay,
        NeckLinkQuicProtocol,
        NeckLinkQuicServer,
        connect_necklink_quic,
        generate_ephemeral_quic_cert,
    )
except ImportError:  # Optional dependency (`pip install aioquic`)
    LossyUDPRelay = None  # type: ignore[assignment,misc]
    NeckLinkQuicProtocol = None  # type: ignore[assignment,misc]
    NeckLinkQuicServer = None  # type: ignore[assignment,misc]
    connect_necklink_quic = None  # type: ignore[assignment]
    generate_ephemeral_quic_cert = None  # type: ignore[assignment]

__version__ = "1.2.0"

__all__ = [
    "DROSOPHILA_NECK_PRIORS",
    "build_epistatic_cryptic_matrix_q8",
    "measure_empirical_connectome_neck_stats",
    "NeckLinkConfig",
    "NeckLinkEncoder",
    "NeckLinkDecoder",
    "NeckLinkSplitBottleneck",
    "c_div",
    "RawFloat32UDPCodec",
    "Int8ZlibBlockCodec",
    "SendOnDeltaUDPCodec",
    "ADPCMLeakyUDPCodec",
    "ReedSolomonGF256Codec",
    "TCPARQAnalyticalEvaluator",
    "simulate_packet_drops",
    "compute_link_economics",
    "export_necklink_c99_header",
    "NeckSocket",
    "NeckUDPServer",
    "AsyncNeckSocket",
    "NLTPServer",
    "NLTPClient",
    "NLTPEndpoint",
    "ReceivedItem",
    "pack_nltp_frame",
    "unpack_nltp_frame",
    "AEADCipherContext",
    "ReplayWindow64",
    "TokenBucketPacer",
    "STUNDiscoveryResult",
    "discover_wan_endpoint",
    "generate_nltp_key",
    "NLTPRelayHub",
    "NeckLinkQuicServer",
    "NeckLinkQuicProtocol",
    "connect_necklink_quic",
    "generate_ephemeral_quic_cert",
    "LossyUDPRelay",
]
