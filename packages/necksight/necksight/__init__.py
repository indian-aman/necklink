"""NeckSight: BANC v888 Connectome-Driven Drosophila Optic Lobe T4/T5, LPLC2/LPi & LC4/Li22 Engine."""

from .egomotion import gyro_to_shift_px, imu_to_ego_motion, shift_grid, warp_grid_3dof
from .emd import MotionOutput, T4T5MotionEngine
from .looming import (
    GiantFiberTrigger,
    LPLC2LoomingReflex,
    NeckSightPipeline,
    attach_nltp_reflex,
    load_banc_optic_weights,
)
from .retina import LaminaOutput, RetinaLaminaPreproc

__version__ = "3.0.0"

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
    "load_banc_optic_weights",
    "gyro_to_shift_px",
    "imu_to_ego_motion",
    "shift_grid",
    "warp_grid_3dof",
]
