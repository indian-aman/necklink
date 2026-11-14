"""NeckCompass: Drosophila Central Complex ring-attractor compass & GPS-denied path integrator."""

from .path_integrator import FanBodyNavigator
from .ring_attractor import (
    BANC_CONNECTOME_COUNTS,
    CompassDiagnostics,
    PlasticLandmarkRing,
    RingCompass,
    basin_sigma_for_noise,
    circular_error_rad,
    load_banc_connectome,
    wrap_angle,
)
from .tilt_ahrs import (
    TiltCompensatedAHRS,
    TiltCompensatedCompass,
    TiltDiagnostics,
    synthesize_9dof_frame,
)

__version__ = "3.0.0"

__all__ = [
    "__version__",
    "BANC_CONNECTOME_COUNTS",
    "CompassDiagnostics",
    "FanBodyNavigator",
    "PlasticLandmarkRing",
    "RingCompass",
    "TiltCompensatedAHRS",
    "TiltCompensatedCompass",
    "TiltDiagnostics",
    "basin_sigma_for_noise",
    "circular_error_rad",
    "load_banc_connectome",
    "synthesize_9dof_frame",
    "wrap_angle",
]
