"""NeckSight: Drosophila Optic Lobe T4/T5 Motion, LPLC2/LC4 Looming & DNa02 Avoidance Engine."""

from .egomotion import gyro_to_shift_px, imu_to_ego_motion, shift_grid, warp_grid_3dof
from .emd import MotionOutput, T4T5MotionEngine
from .looming import (
    GiantFiberTrigger,
    LPLC2LoomingReflex,
    NeckSightPipeline,
    attach_nltp_reflex,
)
from .retina import LaminaOutput, RetinaLaminaPreproc

__version__ = "2.0.0"

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
    "gyro_to_shift_px",
    "imu_to_ego_motion",
    "shift_grid",
    "warp_grid_3dof",
]
