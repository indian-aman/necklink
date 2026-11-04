"""NeckCompass: Drosophila Central Complex ring-attractor compass & GPS-denied path integrator."""

from .path_integrator import FanBodyNavigator
from .ring_attractor import (
    CompassDiagnostics,
    PlasticLandmarkRing,
    RingCompass,
    circular_error_rad,
    wrap_angle,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "CompassDiagnostics",
    "FanBodyNavigator",
    "PlasticLandmarkRing",
    "RingCompass",
    "circular_error_rad",
    "wrap_angle",
]
