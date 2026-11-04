"""Mushroom Body Lobe plasticity: 1-shot novelty & associative valence learning.

Implements two canonical *Drosophila* Mushroom Body output neuron (MBON)
readout circuits operating over sparse Kenyon cell (KC) codes:

1. :class:`MushroomNoveltyDetector` (`MBON-alpha'3` compartment):
   Rapid 1-shot anti-Hebbian synaptic depression on active KC synapses with
   optional slow exponential recovery (`tau_recover`), producing calibrated
   novelty/anomaly scores in `[0, 1]`.
2. :class:`MushroomValenceClassifier` (`DAN -> KC -> MBON` compartments):
   Online 1-shot and few-shot reward/valence-modulated associative classifier
   with homeostatic synaptic normalization and cross-compartment opponent
   suppression of shared background KCs.

References:
    - Hattori, D. et al. (2017). Representations of novelty and familiarity in
      a mushroom body compartment. Cell, 169(5), 956-969.
    - Aso, Y. et al. (2014). Mushroom body output neurons encode valence and
      guide memory-based action selection in Drosophila. eLife, 3, e04580.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .antennal import AntennalNormalizer, Q16_ONE, float_to_q16
from .kenyon import KenyonHasher


class MushroomNoveltyDetector:
    """Online 1-shot `MBON-alpha'3` anti-Hebbian novelty / anomaly detector.

    All `KC -> MBON-alpha'3` weights initialize at `1.0` (maximally novel).
    When an input activates `k` Kenyon cells, the readout outputs the mean
    synaptic weight across those active KCs in `[0, 1]`, then depresses only
    the active synapses via `w_j <- w_j * (1 - lr)`. Optional slow recovery
    (`tau_recover`) relaxes habituated weights back toward `1.0` over time.

    The default code is 2% sparse (about 20 of 1024 Kenyon cells), sparser than the 5% used
    for classification and search: unrelated inputs then share fewer cells, so a novel
    pattern is less likely to look half-familiar (streaming AUROC 0.961 -> 0.997 on held-out
    seeds of the benchmark in ``scripts/benchmark_necksense.py``).
    """

    def __init__(
        self,
        n_inputs: int | None = None,
        *,
        hasher: KenyonHasher | None = None,
        antennal: AntennalNormalizer | bool | None = None,
        stateful_antennal: bool = False,
        n_kc: int = 1024,
        d_proj: int = 6,
        wta_frac: float = 0.02,
        lr: float = 0.55,
        tau_recover: float | None = None,
        seed: int = 42,
        c_parity: bool = False,
    ) -> None:
        if not (0.0 < lr <= 1.0):
            raise ValueError("lr must be in (0, 1]")
        if tau_recover is not None and tau_recover <= 0.0:
            raise ValueError("tau_recover must be > 0 or None")

        if hasher is None:
            if n_inputs is None:
                raise ValueError("Provide either `n_inputs` or `hasher`")
            hasher = KenyonHasher(
                n_inputs,
                n_kc=n_kc,
                d_proj=d_proj,
                wta_frac=wta_frac,
                seed=seed,
                c_parity=c_parity,
            )
        self.hasher = hasher
        self.c_parity = bool(c_parity or hasher.c_parity)
        self.stateful_antennal = bool(stateful_antennal)

        if isinstance(antennal, AntennalNormalizer):
            self.antennal: AntennalNormalizer | None = antennal
        elif antennal is True:
            self.antennal = AntennalNormalizer(self.hasher.n_inputs, c_parity=self.c_parity)
        else:
            self.antennal = None

        self.lr = float(lr)
        self.tau_recover = None if tau_recover is None else float(tau_recover)
        self._rec_alpha = 0.0 if self.tau_recover is None else min(1.0, 1.0 / self.tau_recover)

        self._keep_q16 = int(round((1.0 - self.lr) * Q16_ONE))
        self._rec_q16 = int(round(self._rec_alpha * Q16_ONE))

        self.weights = np.ones(self.hasher.n_kc, dtype=np.float64)
        self._weights_q16 = np.full(self.hasher.n_kc, Q16_ONE, dtype=np.int32)
        self.steps = 0

    def reset(self) -> None:
        """Reset all `KC -> MBON-alpha'3` synaptic weights to `1.0`."""
        self.weights.fill(1.0)
        self._weights_q16.fill(Q16_ONE)
        self.steps = 0
        if self.antennal is not None:
            self.antennal.reset()

    def _encode_indices(self, x: np.ndarray) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if self.antennal is not None:
            arr = self.antennal.step(arr) if self.stateful_antennal else self.antennal.normalize(arr)
        return self.hasher.active_indices(arr)

    def score_indices(self, active_indices: np.ndarray) -> float:
        """Compute calibrated novelty in `[0, 1]` without modifying weights."""
        idx = np.asarray(active_indices, dtype=np.intp)
        if idx.size == 0:
            return 0.0
        if self.c_parity:
            mean_q16 = int(np.sum(self._weights_q16[idx], dtype=np.int64)) // int(idx.size)
            return float(np.clip(mean_q16 / float(Q16_ONE), 0.0, 1.0))
        return float(np.clip(np.mean(self.weights[idx]), 0.0, 1.0))

    def score_tag(self, tag: np.ndarray) -> float:
        """Compute novelty in `[0, 1]` from a packed `uint64` Kenyon tag."""
        return self.score_indices(KenyonHasher.unpack_indices(tag))

    def score(self, x: np.ndarray) -> float:
        """Score 1D sample `x` (or 2D batch if passed via :meth:`score_batch`) without updating."""
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 1:
            raise ValueError("score(x) expects a 1D vector; use score_stream(X) for 2D streams")
        if self.antennal is not None:
            cond = self.antennal.normalize(arr)
            return self.score_indices(self.hasher.active_indices(cond))
        return self.score_indices(self.hasher.active_indices(arr))

    def update_and_score_indices(self, active_indices: np.ndarray) -> float:
        """Apply optional recovery, score active KCs, then depress active synapses."""
        idx = np.asarray(active_indices, dtype=np.intp)
        if idx.size == 0:
            self.steps += 1
            return 0.0

        if self.c_parity:
            if self._rec_q16 > 0:
                diff = Q16_ONE - self._weights_q16.astype(np.int64)
                self._weights_q16 = (self._weights_q16 + ((diff * self._rec_q16) >> 16)).astype(np.int32)
            sum_q16 = int(np.sum(self._weights_q16[idx], dtype=np.int64))
            score_q16 = sum_q16 // int(idx.size)
            self._weights_q16[idx] = (
                (self._weights_q16[idx].astype(np.int64) * self._keep_q16) >> 16
            ).astype(np.int32)
            self.weights[:] = self._weights_q16.astype(np.float64) / float(Q16_ONE)
            self.steps += 1
            return float(np.clip(score_q16 / float(Q16_ONE), 0.0, 1.0))

        if self._rec_alpha > 0.0:
            self.weights += self._rec_alpha * (1.0 - self.weights)
        novelty = float(np.clip(np.mean(self.weights[idx]), 0.0, 1.0))
        self.weights[idx] *= 1.0 - self.lr
        self.steps += 1
        return novelty

    def update_and_score_tag(self, tag: np.ndarray) -> float:
        """Score and update directly from a packed `uint64` Kenyon tag."""
        return self.update_and_score_indices(KenyonHasher.unpack_indices(tag))

    def update_and_score(self, x: np.ndarray) -> float:
        """Score a 1D input `x` in `[0, 1]` and habituate its active KC synapses."""
        return self.update_and_score_indices(self._encode_indices(x))

    def observe(self, x: np.ndarray) -> float:
        """Alias for :meth:`update_and_score`."""
        return self.update_and_score(x)

    def score_stream(self, X: np.ndarray, *, reset_state: bool = False) -> np.ndarray:
        """Sequentially run :meth:`update_and_score` across a `(T, D)` stream."""
        if reset_state:
            self.reset()
        arr = np.asarray(X, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != self.hasher.n_inputs:
            raise ValueError(f"Expected 2D array of shape (T, {self.hasher.n_inputs})")

        # Fast path when no stateful antennal normalizer is attached: batch-encode KCs once
        if self.antennal is None:
            all_idx = self.hasher.active_indices(arr)
            scores = np.empty(arr.shape[0], dtype=np.float64)
            for t in range(arr.shape[0]):
                scores[t] = self.update_and_score_indices(all_idx[t])
            return scores

        scores = np.empty(arr.shape[0], dtype=np.float64)
        for t in range(arr.shape[0]):
            scores[t] = self.update_and_score(arr[t])
        return scores


class MushroomValenceClassifier:
    """1-shot / few-shot `DAN -> KC -> MBON` associative prototype & valence classifier.

    Supports multi-class odor/event identification as well as positive/negative
    valence conditioning (`reward > 0` appetitive, `reward < 0` aversive).
    """

    def __init__(
        self,
        n_inputs: int | None = None,
        *,
        hasher: KenyonHasher | None = None,
        antennal: AntennalNormalizer | bool | None = None,
        stateful_antennal: bool = False,
        n_kc: int = 1024,
        d_proj: int = 6,
        wta_frac: float = 0.05,
        min_lr: float = 0.10,
        plasticity_lr: float | None = None,
        opponent_inh: float = 0.15,
        seed: int = 42,
        c_parity: bool = False,
    ) -> None:
        if not (0.0 < min_lr <= 1.0):
            raise ValueError("min_lr must be in (0, 1]")
        if plasticity_lr is not None and not (0.0 < plasticity_lr <= 1.0):
            raise ValueError("plasticity_lr must be in (0, 1] or None")
        if not (0.0 <= opponent_inh <= 1.0):
            raise ValueError("opponent_inh must be in [0, 1]")

        if hasher is None:
            if n_inputs is None:
                raise ValueError("Provide either `n_inputs` or `hasher`")
            hasher = KenyonHasher(
                n_inputs,
                n_kc=n_kc,
                d_proj=d_proj,
                wta_frac=wta_frac,
                seed=seed,
                c_parity=c_parity,
            )
        self.hasher = hasher
        self.c_parity = bool(c_parity or hasher.c_parity)
        self.stateful_antennal = bool(stateful_antennal)

        if isinstance(antennal, AntennalNormalizer):
            self.antennal: AntennalNormalizer | None = antennal
        elif antennal is True:
            self.antennal = AntennalNormalizer(self.hasher.n_inputs, c_parity=self.c_parity)
        else:
            self.antennal = None

        self.min_lr = float(min_lr)
        self.plasticity_lr = None if plasticity_lr is None else float(plasticity_lr)
        self.opponent_inh = float(opponent_inh)

        self._min_lr_q16 = int(round(self.min_lr * Q16_ONE))
        self._fixed_lr_q16 = 0 if self.plasticity_lr is None else int(round(self.plasticity_lr * Q16_ONE))
        self._opp_q16 = int(round(self.opponent_inh * Q16_ONE))

        self._labels: list[Any] = []
        self._label_to_slot: dict[Any, int] = {}
        self._counts: list[int] = []
        self._weights = np.empty((0, self.hasher.n_kc), dtype=np.float64)
        self._weights_q16 = np.empty((0, self.hasher.n_kc), dtype=np.int32)

    @property
    def classes(self) -> list[Any]:
        """Ordered list of learned class labels."""
        return list(self._labels)

    def reset(self) -> None:
        """Clear all learned compartments and reset optional AntennalNormalizer."""
        self._labels.clear()
        self._label_to_slot.clear()
        self._counts.clear()
        self._weights = np.empty((0, self.hasher.n_kc), dtype=np.float64)
        self._weights_q16 = np.empty((0, self.hasher.n_kc), dtype=np.int32)
        if self.antennal is not None:
            self.antennal.reset()

    def _ensure_slot(self, label: Any) -> int:
        if label in self._label_to_slot:
            return self._label_to_slot[label]
        slot = len(self._labels)
        self._labels.append(label)
        self._label_to_slot[label] = slot
        self._counts.append(0)
        z_f = np.zeros((1, self.hasher.n_kc), dtype=np.float64)
        z_q = np.zeros((1, self.hasher.n_kc), dtype=np.int32)
        self._weights = z_f if slot == 0 else np.vstack((self._weights, z_f))
        self._weights_q16 = z_q if slot == 0 else np.vstack((self._weights_q16, z_q))
        return slot

    def _encode_indices(self, x: np.ndarray, *, stateful: bool) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if self.antennal is not None:
            arr = self.antennal.step(arr) if stateful else self.antennal.normalize(arr)
        return self.hasher.active_indices(arr)

    def learn_indices(self, active_indices: np.ndarray, label: Any, reward: float = 1.0) -> None:
        """Update `KC -> MBON` compartment weights directly from active KC indices."""
        slot = self._ensure_slot(label)
        self._counts[slot] += 1
        n_c = self._counts[slot]
        idx = np.asarray(active_indices, dtype=np.intp)

        if self.c_parity:
            if self._fixed_lr_q16 > 0:
                eta_q16 = self._fixed_lr_q16
            else:
                eta_q16 = max(self._min_lr_q16, Q16_ONE // n_c)
            rew_q16 = int(float_to_q16(reward))
            keep_q16 = Q16_ONE - eta_q16

            row_q = self._weights_q16[slot].astype(np.int64)
            row_q = (row_q * keep_q16) >> 16
            delta_q = (eta_q16 * rew_q16) >> 16
            row_q[idx] += delta_q
            self._weights_q16[slot] = np.clip(row_q, -(1 << 31), (1 << 31) - 1).astype(np.int32)

            if rew_q16 > 0 and self._opp_q16 > 0 and len(self._labels) > 1:
                opp_step_q16 = (eta_q16 * self._opp_q16) >> 16
                if opp_step_q16 > 0:
                    for c2 in range(len(self._labels)):
                        if c2 == slot:
                            continue
                        w_sub = self._weights_q16[c2, idx].astype(np.int64)
                        pos_mask = w_sub > 0
                        if np.any(pos_mask):
                            w_sub[pos_mask] -= (w_sub[pos_mask] * opp_step_q16) >> 16
                            self._weights_q16[c2, idx] = w_sub.astype(np.int32)

            self._weights[:] = self._weights_q16.astype(np.float64) / float(Q16_ONE)
            return

        eta = self.plasticity_lr if self.plasticity_lr is not None else max(self.min_lr, 1.0 / float(n_c))
        self._weights[slot] *= 1.0 - eta
        self._weights[slot, idx] += eta * float(reward)

        if reward > 0.0 and self.opponent_inh > 0.0 and len(self._labels) > 1:
            opp_step = eta * self.opponent_inh
            for c2 in range(len(self._labels)):
                if c2 == slot:
                    continue
                cur = self._weights[c2, idx]
                self._weights[c2, idx] = np.where(cur > 0.0, cur * (1.0 - opp_step), cur)

    def learn_tag(self, tag: np.ndarray, label: Any, reward: float = 1.0) -> None:
        """Learn an association from a packed `uint64` Kenyon tag."""
        self.learn_indices(KenyonHasher.unpack_indices(tag), label, reward=reward)

    def learn(self, x: np.ndarray, label: Any, reward: float = 1.0) -> None:
        """Learn a 1-shot or incremental `DAN -> KC -> MBON` association."""
        idx = self._encode_indices(x, stateful=self.stateful_antennal)
        self.learn_indices(idx, label, reward=reward)

    def predict_scores_indices(self, active_indices: np.ndarray) -> dict[Any, float]:
        """Compute per-class MBON valence readouts from active KC indices."""
        if not self._labels:
            raise RuntimeError("MushroomValenceClassifier has no learned classes yet")
        idx = np.asarray(active_indices, dtype=np.intp)
        if idx.size == 0:
            return {lbl: 0.0 for lbl in self._labels}

        if self.c_parity:
            sums_q16 = np.sum(self._weights_q16[:, idx], axis=1, dtype=np.int64)
            scores_q16 = np.trunc(sums_q16 / int(idx.size)).astype(np.int64)
            return {
                lbl: float(scores_q16[i]) / float(Q16_ONE)
                for i, lbl in enumerate(self._labels)
            }

        raw = np.mean(self._weights[:, idx], axis=1)
        return {lbl: float(raw[i]) for i, lbl in enumerate(self._labels)}

    def predict_scores_tag(self, tag: np.ndarray) -> dict[Any, float]:
        """Compute per-class MBON valence scores from a `uint64` Kenyon tag."""
        return self.predict_scores_indices(KenyonHasher.unpack_indices(tag))

    def predict_scores(self, x: np.ndarray) -> dict[Any, float]:
        """Compute `{label: score}` MBON readouts for a 1D sample `x`."""
        idx = self._encode_indices(x, stateful=self.stateful_antennal)
        return self.predict_scores_indices(idx)

    def predict(self, x: np.ndarray) -> Any:
        """Predict the highest-valence class label for a 1D sample `x` (or 2D batch)."""
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim == 2:
            return [self.predict(arr[t]) for t in range(arr.shape[0])]
        scores = self.predict_scores(arr)
        return max(scores.items(), key=lambda kv: kv[1])[0]
