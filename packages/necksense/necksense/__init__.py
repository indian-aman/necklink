"""NeckSense: Drosophila Antennal Lobe, Chordotonal Organ & Mushroom Body edge sensory AI.

Product #2 under the NeckLink fruit-fly brain suite:
    - :class:`VibrationFrontEnd` (16-ch zero-float chordotonal vibration & acoustic extractor)
    - :class:`AntennalNormalizer` (dual-timescale drift adaptation, whiff rate & LN gain)
    - :class:`KenyonHasher` & :class:`KenyonIndex` (zero-multiply sparse FlyHash + Popcount search)
    - :class:`MushroomNoveltyDetector` (1-shot MBON-alpha'3 anti-Hebbian anomaly detector)
    - :class:`MushroomValenceClassifier` (2-compartment KC-gamma STM + KC-alpha/beta LTM classifier)
"""

from .antennal import AntennalNormalizer, Q16_ONE, float_to_q16, q16_to_float
from .kenyon import (
    KenyonHasher,
    KenyonIndex,
    KenyonMatch,
    build_projection_indices,
    load_banc_mb_weights,
    popcount_overlap,
    popcount_u64,
)
from .mushroom import MushroomNoveltyDetector, MushroomValenceClassifier
from .vibration import (
    NS_VIB_CHANNELS,
    VIBRATION_CHANNEL_NAMES,
    VibrationAnalyzer,
    VibrationFrontEnd,
)

__version__ = "3.0.0"

__all__ = [
    "__version__",
    "Q16_ONE",
    "NS_VIB_CHANNELS",
    "VIBRATION_CHANNEL_NAMES",
    "float_to_q16",
    "q16_to_float",
    "VibrationFrontEnd",
    "VibrationAnalyzer",
    "AntennalNormalizer",
    "KenyonHasher",
    "KenyonIndex",
    "KenyonMatch",
    "build_projection_indices",
    "load_banc_mb_weights",
    "popcount_overlap",
    "popcount_u64",
    "MushroomNoveltyDetector",
    "MushroomValenceClassifier",
]
