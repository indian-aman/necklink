"""Mushroom Body Lobe plasticity: 1-shot novelty & `BANC v888` valence learning.

Implements canonical *Drosophila* Mushroom Body output neuron (MBON) readout
circuits operating over sparse Kenyon cell (KC) codes (`BANC v888` / `MaleCNS`):

1. :class:`MushroomNoveltyDetector` (`KCapbp -> MBON-alpha'3` compartment):
   Rapid 1-shot anti-Hebbian synaptic depression on active KC synapses with
   optional slow exponential recovery (`tau_recover`), producing calibrated
   novelty/anomaly scores in `[0, 1]`.
2. :class:`MushroomValenceClassifier` (`DAN -> KCg / KCab -> 104 MBON` compartments):
   Online 1-shot and few-shot reward/valence-modulated associative classifier
   combining fast `gamma`-lobe short-term plasticity (`KCg`, `kc_is_ab == 0`)
   with consolidated `alpha/beta`-lobe anti-forgetting long-term memory (`KCab`,
   `kc_is_ab == 1`) and empirical `KC -> MBON` (`ach + inh`) readout weighting.

References:
    - Bates, A. S. et al. / BANC Consortium (2025). Brain and Nerve Cord (BANC)
      adult female *Drosophila* whole-CNS connectome (`v888`).
    - Hattori, D. et al. (2017). Representations of novelty and familiarity in
      a mushroom body compartment. Cell, 169(5), 956-969.
    - Aso, Y. et al. (2014). Mushroom body output neurons encode valence and
      guide memory-based action selection in Drosophila. eLife, 3, e04580.
    - Cervantes-Sandoval, I. et al. (2017). Reciprocal synapses between
      mushroom body and dopamine neurons form a positive feedback loop
      required for memory consolidation. eLife, 6, e23789.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from .antennal import _INT32_MAX, _INT32_MIN, AntennalNormalizer, Q16_ONE, float_to_q16
from .kenyon import KenyonHasher, _resolve_connectome_weights

_Q14_ONE: int = 1 << 14
_INT16_MIN: int = -(1 << 15)
_INT16_MAX: int = (1 << 15) - 1


class MushroomNoveltyDetector:
    """Online 1-shot `MBON-alpha'3` anti-Hebbian novelty / anomaly detector.

    All `KC -> MBON-alpha'3` weights initialize at `1.0` (maximally novel).
    When an input activates `k` Kenyon cells, the readout outputs the mean
    synaptic weight across those active KCs in `[0, 1]`, then depresses only
    the active synapses via `w_j <- w_j * (1 - lr)`. Optional slow recovery
    (`tau_recover`) relaxes habituated weights back toward `1.0` over time.

    The default code is 2% sparse (about 20 of 1024 Kenyon cells), sparser than the 5% used
    for classification and search: unrelated inputs then share fewer cells, so a novel
    pattern is less likely to look half-familiar.
    """

    PROVENANCE: str = "connectome"
    CHANNEL_MAPPING_PROVENANCE: str = "design_choice"

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
        connectome_weights: str | Mapping[str, Any] | None = "banc_v888",
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
                connectome_weights=connectome_weights,
                c_parity=c_parity,
            )
        self.hasher = hasher
        self.connectome_mode = hasher.connectome_mode
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
        """Score 1D sample `x` without updating synaptic weights."""
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
                self._weights_q16 = np.minimum(
                    Q16_ONE,
                    (self._weights_q16 + ((diff * self._rec_q16) >> 16)).astype(np.int32),
                )
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
    """2-compartment `KC-gamma` (STM) + `KC-alpha/beta` (LTM) associative classifier.

    Models the *Drosophila* Mushroom Body split between fast-adapting `KCg`
    (`gamma` lobe, `kc_is_ab == 0`) and replay-consolidated `KCab` (`alpha/beta`
    lobe, `kc_is_ab == 1`), weighting active KC readouts by empirical `BANC v888`
    `KC -> MBON` (`ach + inh`) synapse counts. Supports 1-shot / few-shot
    multi-class identification, appetitive/aversive valence conditioning, and
    catastrophic-forgetting-free sequential continual learning (`ltm_alpha_beta=True`
    and :meth:`consolidate`).
    """

    PROVENANCE: str = "connectome"
    CHANNEL_MAPPING_PROVENANCE: str = "design_choice"

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
        ltm_alpha_beta: bool = True,
        ltm_floor: float = 0.55,
        seed: int = 42,
        connectome_weights: str | Mapping[str, Any] | None = "banc_v888",
        c_parity: bool = False,
    ) -> None:
        if not (0.0 < min_lr <= 1.0):
            raise ValueError("min_lr must be in (0, 1]")
        if plasticity_lr is not None and not (0.0 < plasticity_lr <= 1.0):
            raise ValueError("plasticity_lr must be in (0, 1] or None")
        if not (0.0 <= opponent_inh <= 1.0):
            raise ValueError("opponent_inh must be in [0, 1]")
        if not (0.0 < ltm_floor <= 1.0):
            raise ValueError("ltm_floor must be in (0, 1]")

        if hasher is None:
            if n_inputs is None:
                raise ValueError("Provide either `n_inputs` or `hasher`")
            hasher = KenyonHasher(
                n_inputs,
                n_kc=n_kc,
                d_proj=d_proj,
                wta_frac=wta_frac,
                seed=seed,
                connectome_weights=connectome_weights,
                c_parity=c_parity,
            )
            eff_cw = connectome_weights
        else:
            if connectome_weights == "banc_v888" and hasher.connectome_mode != "banc_v888":
                eff_cw = hasher._raw_connectome_arg
            else:
                eff_cw = connectome_weights

        self.hasher = hasher
        self.connectome_mode, self._mb_weights = _resolve_connectome_weights(
            eff_cw, self.hasher.n_kc
        )
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
        self.ltm_alpha_beta = bool(ltm_alpha_beta)
        self.ltm_floor = float(ltm_floor)

        self._min_lr_q16 = int(round(self.min_lr * Q16_ONE))
        self._fixed_lr_q16 = 0 if self.plasticity_lr is None else int(round(self.plasticity_lr * Q16_ONE))
        self._opp_q16 = int(round(self.opponent_inh * Q16_ONE))
        self._ltm_floor_q14 = int(round(self.ltm_floor * _Q14_ONE))

        if self._mb_weights is not None:
            m = self.hasher.n_kc
            self._ab_mask_u64 = self._mb_weights["kc_is_ab_mask_u64"][: self.hasher.n_words].copy()
            kc_idx = np.arange(m, dtype=np.uint64)
            w_vals = self._ab_mask_u64[(kc_idx >> np.uint64(6)).astype(np.intp)]
            self._kc_is_ab_bool = ((w_vals >> (kc_idx & np.uint64(63))) & np.uint64(1)).astype(bool)
            ach = self._mb_weights["kc_to_mbon_ach_q8"][:m].astype(np.int64)
            inh = self._mb_weights["kc_to_mbon_inh_q8"][:m].astype(np.int64)
            self._mbon_gain_i64 = ach + inh
            self._mbon_gain_f64 = self._mbon_gain_i64.astype(np.float64)
            self._eff_ltm_floor = min(2.0, 2.0 * self.ltm_floor)
            self._eff_ltm_floor_q14 = min(_INT16_MAX, self._ltm_floor_q14 * 2)
        else:
            self._ab_mask_u64 = np.full(
                self.hasher.n_words, np.uint64(0xFFFFFFFFFFFFFFFF), dtype=np.uint64
            )
            self._kc_is_ab_bool = np.ones(self.hasher.n_kc, dtype=bool)
            self._mbon_gain_i64 = np.ones(self.hasher.n_kc, dtype=np.int64)
            self._mbon_gain_f64 = np.ones(self.hasher.n_kc, dtype=np.float64)
            self._eff_ltm_floor = self.ltm_floor
            self._eff_ltm_floor_q14 = self._ltm_floor_q14

        self._labels: list[Any] = []
        self._label_to_slot: dict[Any, int] = {}
        self._counts: list[int] = []
        self._weights = np.empty((0, self.hasher.n_kc), dtype=np.float64)
        self._ltm_weights = np.empty((0, self.hasher.n_kc), dtype=np.float64)
        self._ltm_mask = np.empty((0, self.hasher.n_kc), dtype=bool)
        self._weights_q14 = np.empty((0, self.hasher.n_kc), dtype=np.int16)
        self.consolidations: int = 0

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
        self._ltm_weights = np.empty((0, self.hasher.n_kc), dtype=np.float64)
        self._ltm_mask = np.empty((0, self.hasher.n_kc), dtype=bool)
        self._weights_q14 = np.empty((0, self.hasher.n_kc), dtype=np.int16)
        self.consolidations = 0
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
        z_b = np.zeros((1, self.hasher.n_kc), dtype=bool)
        z_q = np.zeros((1, self.hasher.n_kc), dtype=np.int16)
        self._weights = z_f if slot == 0 else np.vstack((self._weights, z_f))
        self._ltm_weights = z_f.copy() if slot == 0 else np.vstack((self._ltm_weights, z_f.copy()))
        self._ltm_mask = z_b if slot == 0 else np.vstack((self._ltm_mask, z_b))
        self._weights_q14 = z_q if slot == 0 else np.vstack((self._weights_q14, z_q))
        return slot

    def _encode_indices(self, x: np.ndarray, *, stateful: bool) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if self.antennal is not None:
            arr = self.antennal.step(arr) if stateful else self.antennal.normalize(arr)
        return self.hasher.active_indices(arr)

    def learn_indices(self, active_indices: np.ndarray, label: Any, reward: float = 1.0) -> None:
        """Update `KCg` (and `KCab` LTM) compartment weights from active KC indices."""
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
            rew_q14 = rew_q16 >> 2
            keep_q16 = Q16_ONE - eta_q16
            delta_q14 = (eta_q16 * rew_q14) >> 16
            opp_step_q16 = (eta_q16 * self._opp_q16) >> 16
            floor_q14 = self._eff_ltm_floor_q14

            row_cur = self._weights_q14[slot].astype(np.int64)
            row_dec = (row_cur * keep_q16) >> 16

            if self.ltm_alpha_beta and np.any(self._ltm_mask[slot]):
                locked = self._ltm_mask[slot].copy()
                if idx.size > 0:
                    active_flags = np.zeros(self.hasher.n_kc, dtype=bool)
                    active_flags[idx] = True
                    opp_active = active_flags & (
                        ((row_cur > 0) & (rew_q14 < 0)) | ((row_cur < 0) & (rew_q14 > 0))
                    )
                    locked &= ~opp_active
                pos_prot = locked & (row_cur >= floor_q14) & (row_dec < floor_q14)
                neg_prot = locked & (row_cur <= -floor_q14) & (row_dec > -floor_q14)
                row_dec[pos_prot] = floor_q14
                row_dec[neg_prot] = -floor_q14

            row_dec = np.clip(row_dec, _INT16_MIN, _INT16_MAX)
            if idx.size > 0:
                row_dec[idx] = np.clip(row_dec[idx] + delta_q14, _INT16_MIN, _INT16_MAX)
                if self.ltm_alpha_beta:
                    weak = np.abs(row_dec[idx]) < (floor_q14 >> 1)
                    if np.any(weak):
                        self._ltm_mask[slot, idx[weak]] = False

            self._weights_q14[slot] = row_dec.astype(np.int16)

            if rew_q16 > 0 and opp_step_q16 > 0 and len(self._labels) > 1 and idx.size > 0:
                for c2 in range(len(self._labels)):
                    if c2 == slot:
                        continue
                    sub_idx = idx[~self._ltm_mask[c2, idx]] if self.ltm_alpha_beta else idx
                    if sub_idx.size == 0:
                        continue
                    w_sub = self._weights_q14[c2, sub_idx].astype(np.int64)
                    pos_mask = w_sub > 0
                    if np.any(pos_mask):
                        w_sub[pos_mask] -= (w_sub[pos_mask] * opp_step_q16) >> 16
                        self._weights_q14[c2, sub_idx] = np.clip(
                            w_sub, _INT16_MIN, _INT16_MAX
                        ).astype(np.int16)

            self._weights[:] = self._weights_q14.astype(np.float64) / float(_Q14_ONE)
            return

        # Float mode: fast KC-gamma STM update
        eta = self.plasticity_lr if self.plasticity_lr is not None else max(self.min_lr, 1.0 / float(n_c))
        rew = float(reward)
        eff_floor = self._eff_ltm_floor

        row_prev = self._weights[slot].copy()
        row_next = row_prev * (1.0 - eta)

        if self.ltm_alpha_beta and np.any(self._ltm_mask[slot]):
            locked = self._ltm_mask[slot].copy()
            if idx.size > 0:
                act_mask = np.zeros(self.hasher.n_kc, dtype=bool)
                act_mask[idx] = True
                opp_act = act_mask & (((row_prev > 0.0) & (rew < 0.0)) | ((row_prev < 0.0) & (rew > 0.0)))
                locked &= ~opp_act
            pos_p = locked & (row_prev >= eff_floor) & (row_next < eff_floor)
            neg_p = locked & (row_prev <= -eff_floor) & (row_next > -eff_floor)
            row_next[pos_p] = eff_floor
            row_next[neg_p] = -eff_floor

        if idx.size > 0:
            row_next[idx] = np.clip(row_next[idx] + eta * rew, -2.0, 2.0)
            if self.ltm_alpha_beta:
                weak = np.abs(row_next[idx]) < (0.5 * eff_floor)
                if np.any(weak):
                    self._ltm_mask[slot, idx[weak]] = False
                    self._ltm_weights[slot, idx[weak]] = 0.0
        self._weights[slot] = row_next

        if rew > 0.0 and self.opponent_inh > 0.0 and len(self._labels) > 1 and idx.size > 0:
            opp_step = eta * self.opponent_inh
            for c2 in range(len(self._labels)):
                if c2 == slot:
                    continue
                sub_idx = idx[~self._ltm_mask[c2, idx]] if self.ltm_alpha_beta else idx
                if sub_idx.size == 0:
                    continue
                cur = self._weights[c2, sub_idx]
                self._weights[c2, sub_idx] = np.where(cur > 0.0, cur * (1.0 - opp_step), cur)

    def consolidate(self, thresh: float = 0.50) -> int:
        """Consolidate salient STM traces into `KCab` (`kc_is_ab == 1`) LTM.

        In `banc_v888` / connectome mode, `kc_is_ab_mask_u64` locks consolidated
        long-term memories exclusively onto `KCab` (`alpha/beta` lobe, `bit == 1`)
        while `KCg` (`gamma` lobe, `bit == 0`) transfers its transient STM weight
        and remains fast plastic STM. Matches `ns_valence_consolidate` bit-for-bit
        when `c_parity=True`. Returns total number of consolidated `KCab` synapses.
        """
        if not self.ltm_alpha_beta or not self._labels:
            return 0
        if thresh <= 0.0:
            thresh = 0.50

        is_ab = self._kc_is_ab_bool
        is_gamma = ~is_ab

        if self.c_parity:
            thresh_q16 = int(round(thresh * Q16_ONE))
            thresh_q14 = max(1, thresh_q16 >> 2)
            for c in range(len(self._labels)):
                w_row = self._weights_q14[c].astype(np.int64)
                salient = (w_row >= thresh_q14) | (w_row <= -thresh_q14)
                if self._mb_weights is not None:
                    new_ab = is_ab & (~self._ltm_mask[c]) & salient
                    if np.any(new_ab):
                        w_row[new_ab] = np.clip(w_row[new_ab] * 2, _INT16_MIN, _INT16_MAX)
                        w_row[is_gamma] = w_row[is_gamma] >> 2
                        self._weights_q14[c] = w_row.astype(np.int16)
                        self._ltm_mask[c] |= new_ab
                else:
                    self._ltm_mask[c] |= salient
            self._weights[:] = self._weights_q14.astype(np.float64) / float(_Q14_ONE)
            self._ltm_weights[:] = np.where(self._ltm_mask, self._weights, 0.0)
            self.consolidations += 1
            return int(np.sum(self._ltm_mask))

        for c in range(len(self._labels)):
            w_row = self._weights[c]
            salient = (w_row >= thresh) | (w_row <= -thresh)
            if self._mb_weights is not None:
                new_ab = is_ab & (~self._ltm_mask[c]) & salient
                if np.any(new_ab):
                    w_row[new_ab] = np.clip(w_row[new_ab] * 2.0, -2.0, 2.0)
                    w_row[is_gamma] *= 0.25
                    self._ltm_mask[c] |= new_ab
            else:
                self._ltm_mask[c] |= salient
        self._ltm_weights[:] = np.where(self._ltm_mask, self._weights, 0.0)
        self.consolidations += 1
        return int(np.sum(self._ltm_mask))

    def learn_tag(self, tag: np.ndarray, label: Any, reward: float = 1.0) -> None:
        """Learn an association from a packed `uint64` Kenyon tag."""
        self.learn_indices(KenyonHasher.unpack_indices(tag), label, reward=reward)

    def learn(self, x: np.ndarray, label: Any, reward: float = 1.0) -> None:
        """Learn a 1-shot or incremental `DAN -> KC -> MBON` association."""
        idx = self._encode_indices(x, stateful=self.stateful_antennal)
        self.learn_indices(idx, label, reward=reward)

    def predict_scores_indices(self, active_indices: np.ndarray) -> dict[Any, float]:
        """Compute `KC -> MBON` (`ach + inh`)-weighted per-class valence readouts."""
        if not self._labels:
            raise RuntimeError("MushroomValenceClassifier has no learned classes yet")
        idx = np.asarray(active_indices, dtype=np.intp)
        if idx.size == 0:
            return {lbl: 0.0 for lbl in self._labels}

        if self.c_parity:
            gains_i64 = self._mbon_gain_i64[idx]
            gain_den = int(np.sum(gains_i64, dtype=np.int64))
            if gain_den <= 0:
                return {lbl: 0.0 for lbl in self._labels}
            nums = (self._weights_q14[:, idx].astype(np.int64) @ gains_i64) * 4
            scores_q16 = np.where(nums >= 0, nums // gain_den, -((-nums) // gain_den))
            scores_q16 = np.clip(scores_q16, _INT32_MIN, _INT32_MAX)
            return {
                lbl: float(scores_q16[i]) / float(Q16_ONE)
                for i, lbl in enumerate(self._labels)
            }

        gains_f64 = self._mbon_gain_f64[idx]
        gain_den_f = float(np.sum(gains_f64))
        if gain_den_f <= 0.0:
            return {lbl: 0.0 for lbl in self._labels}
        raw = (self._weights[:, idx] @ gains_f64) / gain_den_f
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
