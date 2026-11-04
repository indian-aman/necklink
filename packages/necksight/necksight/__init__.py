"""NeckSight: Drosophila Optic Lobe T4/T5 Motion & LPLC2 Looming Reflex Engine."""

from .emd import MotionOutput, T4T5MotionEngine
from .looming import (
    GiantFiberTrigger,
    LPLC2LoomingReflex,
    NeckSightPipeline,
    attach_nltp_reflex,
)
from .retina import LaminaOutput, RetinaLaminaPreproc

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "LaminaOutput",
    "RetinaLaminaPreproc",
    "MotionOutput",
    "T4T5MotionEngine",
    "GiantFiberTrigger",
    "LPLC2LoomingReflex",
    "NeckSightPipeline",
    "attach_nltp_reflex",
]
