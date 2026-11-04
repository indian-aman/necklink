"""NeckSense: Drosophila Antennal Lobe + Mushroom Body edge sensory intelligence.

Product #2 under the NeckLink fruit-fly brain suite:
    - :class:`AntennalNormalizer` (dual-timescale drift adaptation + Olsen-Wilson LN gain)
    - :class:`KenyonHasher` & :class:`KenyonIndex` (zero-multiply sparse FlyHash + Popcount search)
    - :class:`MushroomNoveltyDetector` (1-shot MBON-alpha'3 anti-Hebbian anomaly detector)
    - :class:`MushroomValenceClassifier` (1-shot / few-shot DAN->KC->MBON classifier)
"""

from .antennal import AntennalNormalizer, Q16_ONE, float_to_q16, q16_to_float
from .kenyon import (
    KenyonHasher,
    KenyonIndex,
    KenyonMatch,
    build_projection_indices,
    popcount_overlap,
    popcount_u64,
)
from .mushroom import MushroomNoveltyDetector, MushroomValenceClassifier

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "Q16_ONE",
    "float_to_q16",
    "q16_to_float",
    "AntennalNormalizer",
    "KenyonHasher",
    "KenyonIndex",
    "KenyonMatch",
    "build_projection_indices",
    "popcount_overlap",
    "popcount_u64",
    "MushroomNoveltyDetector",
    "MushroomValenceClassifier",
]
