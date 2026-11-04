"""Mushroom Body Calyx sparse binary projection and APL winner-take-all hashing.

Implements the Drosophila projection-neuron-to-Kenyon-cell (PN -> KC) sparse
expansion (`d_proj=6` random inputs summed per KC with zero floating-point
multiplications) followed by anterior paired lateral (APL) global inhibition
keeping strictly the top-`k` winners packed into `uint64` bitwords.

Reference:
    - Dasgupta, S., Stevens, C. F. & Navlakha, S. (2017). A neural algorithm
      for a fundamental computing problem. Science, 358(6364), 793-796.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple, Sequence

import numpy as np

from .antennal import float_to_q16

_HAS_BITWISE_COUNT: bool = hasattr(np, "bitwise_count")

_SWAR_M1 = np.uint64(0x5555555555555555)
_SWAR_M2 = np.uint64(0x3333333333333333)
_SWAR_M4 = np.uint64(0x0F0F0F0F0F0F0F0F)
_SWAR_H01 = np.uint64(0x0101010101010101)


class KenyonMatch(NamedTuple):
    """Nearest-neighbor match returned by :meth:`KenyonIndex.query`."""

    id: Any
    overlap: int
    similarity: float
    index: int


def popcount_u64(words: np.ndarray) -> np.ndarray:
    """Count set bits in each element of a `uint64` array (`numpy>=1.24` safe)."""
    w = np.asarray(words, dtype=np.uint64)
    if _HAS_BITWISE_COUNT:
        return np.bitwise_count(w)
    x = w - ((w >> np.uint64(1)) & _SWAR_M1)
    x = (x & _SWAR_M2) + ((x >> np.uint64(2)) & _SWAR_M2)
    x = (x + (x >> np.uint64(4))) & _SWAR_M4
    return ((x * _SWAR_H01) >> np.uint64(56)).astype(np.uint8)


def popcount_overlap(tag_a: np.ndarray, tag_b: np.ndarray) -> np.ndarray | int:
    """Compute bitwise AND popcount overlap between `uint64` Kenyon tags.

    Supports `(W,)` vs `(W,)` returning `int`, or `(N, W)` vs `(W,)` / `(N, W)`
    returning an `int32` array of length `N`.
    """
    a = np.asarray(tag_a, dtype=np.uint64)
    b = np.asarray(tag_b, dtype=np.uint64)
    overlap_counts = popcount_u64(a & b)
    if a.ndim == 1 and b.ndim == 1:
        return int(np.sum(overlap_counts, dtype=np.int32))
    return np.sum(overlap_counts, axis=-1, dtype=np.int32)


def build_projection_indices(n_inputs: int, n_kc: int, d_proj: int, seed: int) -> np.ndarray:
    """Generate deterministic `(n_kc, d_proj)` PN->KC claw indices matching C99.

    Uses exact 32-bit `xorshift32` + divisionless Lemire range mapping
    `((uint64_t)state * (uint64_t)n_inputs) >> 32`, matching `ns_kenyon_hash`
    in `csrc/necksense.c` bit-for-bit.
    """
    state = (int(seed) & 0xFFFFFFFF) ^ 0x9E3779B9
    if state == 0:
        state = 0x6D2B79F5

    total = n_kc * d_proj
    flat = np.empty(total, dtype=np.int32)
    d_u64 = int(n_inputs)
    for idx in range(total):
        state = (state ^ ((state << 13) & 0xFFFFFFFF)) & 0xFFFFFFFF
        state = (state ^ (state >> 17)) & 0xFFFFFFFF
        state = (state ^ ((state << 5) & 0xFFFFFFFF)) & 0xFFFFFFFF
        flat[idx] = (state * d_u64) >> 32
    return flat.reshape(n_kc, d_proj)


class KenyonHasher:
    """Sparse binary FlyHash projection + APL global winner-take-all encoder.

    Expands `D` sensory inputs into `M` Kenyon cells (`M` a multiple of 64) by
    summing `d_proj` deterministically chosen inputs per KC (**zero float
    multiplications**), then applies `APL` winner-take-all inhibition keeping
    strictly `k_winners` active bits packed into `M // 64` `uint64` words.

    Parameters
    ----------
    n_inputs : int
        Input vector dimensionality (`D >= 1`).
    n_kc : int, default=1024
        Number of Kenyon cells (`M >= 64`, must be divisible by `64`). `1024`
        produces a `128 B` tag (`16 x uint64`); `2048` produces a `256 B` tag.
    d_proj : int, default=6
        Number of PN claw inputs summed per Kenyon cell (`d_proj >= 1`).
    wta_frac : float, default=0.05
        Fraction of top-responding Kenyon cells kept active (`0 < wta_frac <= 1`).
    k_winners : int | None, default=None
        Optional explicit winner count overriding `round(n_kc * wta_frac)`.
    seed : int, default=42
        Deterministic 32-bit connectivity seed.
    c_parity : bool, default=False
        When `True`, quantizes inputs to `Q16.16` `int32` and performs integer
        accumulation identical to `ns_kenyon_hash` in `csrc/necksense.c`.
    """

    def __init__(
        self,
        n_inputs: int,
        *,
        n_kc: int = 1024,
        d_proj: int = 6,
        wta_frac: float = 0.05,
        k_winners: int | None = None,
        seed: int = 42,
        c_parity: bool = False,
    ) -> None:
        if n_inputs < 1:
            raise ValueError("n_inputs must be >= 1")
        if n_kc < 64 or (n_kc % 64) != 0:
            raise ValueError("n_kc must be >= 64 and a multiple of 64")
        if d_proj < 1:
            raise ValueError("d_proj must be >= 1")
        if not (0.0 < wta_frac <= 1.0):
            raise ValueError("wta_frac must be in (0, 1]")

        self.n_inputs = int(n_inputs)
        self.n_kc = int(n_kc)
        self.n_words = self.n_kc // 64
        self.tag_bytes = self.n_words * 8
        self.d_proj = int(d_proj)
        self.wta_frac = float(wta_frac)
        if k_winners is None:
            self.k_winners = max(1, min(self.n_kc, int(round(self.n_kc * self.wta_frac))))
        else:
            if not (1 <= k_winners <= self.n_kc):
                raise ValueError("k_winners must be in [1, n_kc]")
            self.k_winners = int(k_winners)
        self.seed = int(seed) & 0xFFFFFFFF
        self.c_parity = bool(c_parity)

        self.proj_indices = build_projection_indices(
            self.n_inputs, self.n_kc, self.d_proj, self.seed
        )

    def _project_1d(self, x: np.ndarray) -> np.ndarray:
        """Compute 1D KC sums using pure additions (zero multiplications)."""
        if self.c_parity:
            xq = float_to_q16(x).astype(np.int64)
            act = xq[self.proj_indices[:, 0]].copy()
            for c in range(1, self.d_proj):
                act += xq[self.proj_indices[:, c]]
            return act
        xf = np.asarray(x, dtype=np.float64)
        act = xf[self.proj_indices[:, 0]].copy()
        for c in range(1, self.d_proj):
            act += xf[self.proj_indices[:, c]]
        return act

    def _project_2d(self, X: np.ndarray) -> np.ndarray:
        """Compute 2D `(N, n_kc)` KC sums using pure in-place vector additions."""
        if self.c_parity:
            xq = float_to_q16(X).astype(np.int64)
            act = xq[:, self.proj_indices[:, 0]].copy()
            for c in range(1, self.d_proj):
                act += xq[:, self.proj_indices[:, c]]
            return act
        xf = np.asarray(X, dtype=np.float64)
        act = xf[:, self.proj_indices[:, 0]].copy()
        for c in range(1, self.d_proj):
            act += xf[:, self.proj_indices[:, c]]
        return act

    @staticmethod
    def _select_top_k_1d(act: np.ndarray, k: int) -> np.ndarray:
        """Select exact top-k KC indices breaking ties toward smaller KC index."""
        if k == act.shape[0]:
            return np.arange(k, dtype=np.int32)
        thresh = -np.partition(-act, k - 1)[k - 1]
        strict = np.flatnonzero(act > thresh)
        rem = k - strict.size
        if rem <= 0:
            return strict[:k].astype(np.int32)
        ties = np.flatnonzero(act == thresh)[:rem]
        if strict.size == 0:
            return ties.astype(np.int32)
        return np.concatenate((strict, ties)).astype(np.int32)

    def _select_top_k_2d(self, act: np.ndarray) -> np.ndarray:
        """Select exact `(N, k)` winner indices across a batch."""
        k = self.k_winners
        n_rows, m = act.shape
        if k == m:
            return np.broadcast_to(np.arange(m, dtype=np.int32), (n_rows, m)).copy()

        neg = -act
        part_idx = np.argpartition(neg, k - 1, axis=1)
        winners = part_idx[:, :k].astype(np.int32, copy=True)

        row_ids = np.arange(n_rows)
        thresh = act[row_ids, part_idx[:, k - 1]][:, None]
        ge_counts = np.sum(act >= thresh, axis=1)
        tie_rows = np.flatnonzero(ge_counts > k)
        for r in tie_rows:
            winners[r] = self._select_top_k_1d(act[r], k)
        return winners

    def active_indices(self, x: np.ndarray) -> np.ndarray:
        """Return active winner KC indices `(k,)` or `(N, k)` (`int32`)."""
        arr = np.asarray(x)
        if arr.ndim == 1:
            if arr.shape[0] != self.n_inputs:
                raise ValueError(f"Expected {self.n_inputs} inputs, got {arr.shape[0]}")
            return self._select_top_k_1d(self._project_1d(arr), self.k_winners)
        if arr.ndim == 2:
            if arr.shape[1] != self.n_inputs:
                raise ValueError(f"Expected {self.n_inputs} inputs, got {arr.shape[1]}")
            return self._select_top_k_2d(self._project_2d(arr))
        raise ValueError("x must be 1D (D,) or 2D (N, D)")

    def pack_indices(self, winners: np.ndarray) -> np.ndarray:
        """Pack winner indices `(k,)` or `(N, k)` into `uint64` bitwords."""
        w = np.asarray(winners, dtype=np.int64)
        if w.ndim == 1:
            words = np.zeros(self.n_words, dtype=np.uint64)
            word_idx = w >> 6
            bit_mask = np.uint64(1) << (w & 63).astype(np.uint64)
            np.bitwise_or.at(words, word_idx, bit_mask)
            return words
        if w.ndim == 2:
            n_rows = w.shape[0]
            words = np.zeros((n_rows, self.n_words), dtype=np.uint64)
            row_idx = np.arange(n_rows, dtype=np.intp)[:, None]
            word_idx = (w >> 6).astype(np.intp)
            bit_mask = np.uint64(1) << (w & 63).astype(np.uint64)
            np.bitwise_or.at(words, (row_idx, word_idx), bit_mask)
            return words
        raise ValueError("winners must be 1D (k,) or 2D (N, k)")

    def hash_q16(self, x_q16: np.ndarray) -> np.ndarray:
        """Hash pre-quantized Q16.16 `int32` vector(s) with exact C99 parity."""
        arr = np.asarray(x_q16, dtype=np.int64)
        if arr.ndim == 1:
            if arr.shape[0] != self.n_inputs:
                raise ValueError(f"Expected {self.n_inputs} inputs, got {arr.shape[0]}")
            act = arr[self.proj_indices[:, 0]].copy()
            for c in range(1, self.d_proj):
                act += arr[self.proj_indices[:, c]]
            return self.pack_indices(self._select_top_k_1d(act, self.k_winners))
        if arr.ndim == 2:
            if arr.shape[1] != self.n_inputs:
                raise ValueError(f"Expected {self.n_inputs} inputs, got {arr.shape[1]}")
            act = arr[:, self.proj_indices[:, 0]].copy()
            for c in range(1, self.d_proj):
                act += arr[:, self.proj_indices[:, c]]
            return self.pack_indices(self._select_top_k_2d(act))
        raise ValueError("x_q16 must be 1D (D,) or 2D (N, D)")

    def hash(self, x: np.ndarray) -> np.ndarray:
        """Encode 1D `(D,)` or 2D `(N, D)` input(s) into `uint64` bitwords."""
        return self.pack_indices(self.active_indices(x))

    @staticmethod
    def unpack_indices(tag: np.ndarray) -> np.ndarray:
        """Extract sorted active KC indices from a 1D `(W,)` `uint64` tag."""
        words = np.asarray(tag, dtype=np.uint64)
        if words.ndim != 1:
            raise ValueError("tag must be a 1D uint64 array")
        out: list[int] = []
        for w_idx in range(words.shape[0]):
            val = int(words[w_idx])
            base = w_idx << 6
            while val:
                lsb = val & -val
                bit = lsb.bit_length() - 1
                out.append(base + bit)
                val ^= lsb
        return np.asarray(out, dtype=np.int32)

    @staticmethod
    def tag_to_hex(tag: np.ndarray) -> str:
        """Format a 1D `uint64` tag as a big-endian-word hex string."""
        words = np.asarray(tag, dtype=np.uint64)
        if words.ndim != 1:
            raise ValueError("tag must be a 1D uint64 array")
        return "".join(f"{int(w):016x}" for w in words)

    def to_config_dict(self) -> dict[str, Any]:
        """Return JSON-serializable configuration dictionary."""
        return {
            "n_inputs": self.n_inputs,
            "n_kc": self.n_kc,
            "d_proj": self.d_proj,
            "wta_frac": self.wta_frac,
            "k_winners": self.k_winners,
            "seed": self.seed,
            "c_parity": self.c_parity,
        }

    @classmethod
    def from_config_dict(cls, cfg: dict[str, Any]) -> KenyonHasher:
        """Reconstruct a :class:`KenyonHasher` from a config dictionary."""
        return cls(
            int(cfg["n_inputs"]),
            n_kc=int(cfg["n_kc"]),
            d_proj=int(cfg["d_proj"]),
            wta_frac=float(cfg["wta_frac"]),
            k_winners=int(cfg["k_winners"]),
            seed=int(cfg["seed"]),
            c_parity=bool(cfg.get("c_parity", False)),
        )


class KenyonIndex:
    """Vectorized bitset AND/Popcount similarity search index over Kenyon tags."""

    def __init__(
        self,
        hasher: KenyonHasher | None = None,
        *,
        n_inputs: int | None = None,
        n_kc: int = 1024,
        d_proj: int = 6,
        wta_frac: float = 0.05,
        k_winners: int | None = None,
        seed: int = 42,
        c_parity: bool = False,
    ) -> None:
        if hasher is None:
            if n_inputs is None:
                raise ValueError("Provide either `hasher` or `n_inputs`")
            hasher = KenyonHasher(
                n_inputs,
                n_kc=n_kc,
                d_proj=d_proj,
                wta_frac=wta_frac,
                k_winners=k_winners,
                seed=seed,
                c_parity=c_parity,
            )
        self.hasher = hasher
        self._tags = np.empty((0, self.hasher.n_words), dtype=np.uint64)
        self._tags_t: np.ndarray | None = None
        self._ids: list[Any] = []

    def __len__(self) -> int:
        return int(self._tags.shape[0])

    @property
    def tags(self) -> np.ndarray:
        """Packed `(N, W)` `uint64` binary tag table."""
        return self._tags

    @property
    def memory_bytes(self) -> int:
        """Byte footprint of stored `uint64` binary tags (`N * tag_bytes`)."""
        return int(self._tags.nbytes)

    def add(
        self,
        vectors: np.ndarray,
        ids: Sequence[Any] | np.ndarray | None = None,
    ) -> np.ndarray:
        """Hash and index a 1D vector `(D,)` or 2D batch `(N, D)`."""
        arr = np.asarray(vectors)
        single = arr.ndim == 1
        batch = arr[None, :] if single else arr
        tags = self.hasher.hash(batch)
        self.add_tags(tags, ids=ids)
        return tags[0] if single else tags

    def add_tags(
        self,
        tags: np.ndarray,
        ids: Sequence[Any] | np.ndarray | None = None,
    ) -> None:
        """Append pre-computed `uint64` Kenyon tags `(W,)` or `(N, W)`."""
        t = np.asarray(tags, dtype=np.uint64)
        if t.ndim == 1:
            t = t[None, :]
        if t.ndim != 2 or t.shape[1] != self.hasher.n_words:
            raise ValueError(f"Expected tag width {self.hasher.n_words}, got {t.shape}")

        n_new = t.shape[0]
        base = len(self._ids)
        if ids is None:
            new_ids = list(range(base, base + n_new))
        else:
            new_ids = list(ids)
            if len(new_ids) != n_new:
                raise ValueError(f"Expected {n_new} ids, got {len(new_ids)}")

        if self._tags.shape[0] == 0:
            self._tags = t.copy()
        else:
            self._tags = np.vstack((self._tags, t))
        self._tags_t = None
        self._ids.extend(new_ids)

    def query_raw_tag(self, q_tag: np.ndarray, top_k: int = 10) -> tuple[np.ndarray, np.ndarray]:
        """Return `(row_indices, overlaps)` for top-k matches to a `uint64` tag."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        n_items = self._tags.shape[0]
        if n_items == 0:
            return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)

        q = np.asarray(q_tag, dtype=np.uint64)
        if q.ndim != 1 or q.shape[0] != self.hasher.n_words:
            raise ValueError(f"Expected 1D tag of length {self.hasher.n_words}")

        if self._tags_t is None:
            self._tags_t = np.ascontiguousarray(self._tags.T)

        overlaps = np.zeros(n_items, dtype=np.int32)
        for w_idx in range(self.hasher.n_words):
            qw = q[w_idx]
            if qw != 0:
                overlaps += popcount_u64(self._tags_t[w_idx] & qw)
        k_eff = min(int(top_k), n_items)
        if k_eff < n_items:
            cand = np.argpartition(-overlaps, k_eff - 1)[:k_eff]
            # Sort top-k by descending overlap, breaking ties by ascending index
            order = np.lexsort((cand, -overlaps[cand]))
            best = cand[order]
        else:
            best = np.lexsort((np.arange(n_items), -overlaps))
        return best.astype(np.int32), overlaps[best]

    def query_tag(self, q_tag: np.ndarray, top_k: int = 10) -> list[KenyonMatch]:
        """Query the index using a pre-hashed 1D `uint64` tag."""
        indices, overlaps = self.query_raw_tag(q_tag, top_k=top_k)
        denom = float(self.hasher.k_winners)
        return [
            KenyonMatch(
                id=self._ids[int(idx)],
                overlap=int(ov),
                similarity=float(ov) / denom,
                index=int(idx),
            )
            for idx, ov in zip(indices, overlaps)
        ]

    def query(self, vec: np.ndarray, top_k: int = 10) -> list[KenyonMatch]:
        """Hash `vec` and return top-k matches ordered by KC bit overlap."""
        q_tag = self.hasher.hash(vec)
        return self.query_tag(q_tag, top_k=top_k)

    def save(self, path: str | Path) -> Path:
        """Serialize hasher configuration, tags, and IDs cleanly (no pickle)."""
        out_path = Path(path)
        if out_path.suffix.lower() != ".npz":
            out_path = out_path.with_suffix(".npz")
        meta = {
            "format": "necksense.kenyon_index.v1",
            "hasher": self.hasher.to_config_dict(),
            "ids": self._ids,
        }
        meta_bytes = np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8)
        np.savez_compressed(out_path, tags=self._tags, meta=meta_bytes)
        return out_path

    @classmethod
    def load(cls, path: str | Path) -> KenyonIndex:
        """Load a serialized :class:`KenyonIndex` without `allow_pickle`."""
        in_path = Path(path)
        if not in_path.exists() and in_path.with_suffix(".npz").exists():
            in_path = in_path.with_suffix(".npz")
        with np.load(in_path, allow_pickle=False) as archive:
            meta_raw = archive["meta"].tobytes().decode("utf-8")
            meta = json.loads(meta_raw)
            if meta.get("format") != "necksense.kenyon_index.v1":
                raise ValueError(f"Unsupported KenyonIndex format: {meta.get('format')!r}")
            hasher = KenyonHasher.from_config_dict(meta["hasher"])
            idx = cls(hasher=hasher)
            tags = archive["tags"].astype(np.uint64)
            if tags.shape[0] > 0:
                idx.add_tags(tags, ids=meta["ids"])
            return idx
