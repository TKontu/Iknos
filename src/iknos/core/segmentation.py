import re
from dataclasses import dataclass
from typing import Any, Protocol

import torch
import torch.nn.functional as F


class _PoolingContext(Protocol):
    """The slice of :class:`~iknos.core.embeddings.DocumentContext` segmentation needs.

    Structural (not an import) so this pure module stays free of the heavy
    transformers/torch-model dependency that ``embeddings`` pulls in at import time —
    and so a test can pass any object exposing ``pool_span``.
    """

    def pool_span(self, start_char: int, end_char: int) -> list[float] | None: ...


def calculate_adjacent_similarities(embeddings: list[list[float]]) -> list[float]:
    """
    Calculate cosine similarity between adjacent embeddings.
    """
    if len(embeddings) < 2:
        return []

    tensor_embeddings = torch.tensor(embeddings, dtype=torch.float32)
    # Normalize embeddings
    tensor_embeddings = F.normalize(tensor_embeddings, p=2, dim=1)

    # Dot product of normalized vectors gives cosine similarity
    sims = torch.sum(tensor_embeddings[:-1] * tensor_embeddings[1:], dim=1)

    return sims.tolist()


def smooth_similarities(similarities: list[float], window_size: int = 1) -> list[float]:
    """
    Smooth similarities using a simple moving average.
    """
    if not similarities:
        return []

    smoothed = []
    n = len(similarities)
    for i in range(n):
        start = max(0, i - window_size)
        end = min(n, i + window_size + 1)
        window = similarities[start:end]
        smoothed.append(sum(window) / len(window))
    return smoothed


def find_valleys(similarities: list[float], k: float = 1.0) -> list[int]:
    """
    Find local minima (valleys) in similarities that are deeper than mean - k * std.
    """
    if not similarities or len(similarities) < 3:
        return []

    tensor_sims = torch.tensor(similarities, dtype=torch.float32)
    mean = tensor_sims.mean().item()
    std = tensor_sims.std(unbiased=False).item()
    threshold = mean - k * std

    valleys = []
    for i in range(1, len(similarities) - 1):
        if (
            similarities[i] < similarities[i - 1]
            and similarities[i] < similarities[i + 1]
            and similarities[i] < threshold
        ):
            valleys.append(i)

    return valleys


def calculate_prefix_sums(embeddings: list[list[float]]) -> torch.Tensor:
    """
    Calculate prefix sums of normalized embeddings for O(1) coherence scoring.
    """
    if not embeddings:
        return torch.tensor([])

    tensor_embeddings = torch.tensor(embeddings, dtype=torch.float32)
    tensor_embeddings = F.normalize(tensor_embeddings, p=2, dim=1)

    cumsum = torch.cumsum(tensor_embeddings, dim=0)
    zeros = torch.zeros(1, cumsum.size(1))
    return torch.cat((zeros, cumsum), dim=0)


def calculate_information_density(text: str) -> float:
    """
    Heuristic to estimate information density using numbers, capitalized words, and symbols.
    """
    numbers = len(re.findall(r"\b\d+(?:\.\d+)?\b", text))
    caps = len(re.findall(r"\b[A-Z][A-Za-z0-9]+\b", text))
    symbols = len(re.findall(r"[$%]", text))
    return float(numbers + caps + symbols)


def segment_dp(
    embeddings: list[list[float]],
    valleys: list[int],
    densities: list[float],
    max_len: int = 50,
    penalty_weight: float = 0.1,
    penalty_exponent: float = 1.0,
    density_weight: float = 0.5,
) -> list[tuple[int, int]]:
    """
    Dynamic programming sentence segmentation over candidate valley boundaries.
    """
    N = len(embeddings)
    if N == 0:
        return []

    candidates = [0] + [v for v in valleys if 0 < v < N] + [N]

    final_candidates = [0]
    for c in candidates[1:]:
        while c - final_candidates[-1] > max_len:
            final_candidates.append(final_candidates[-1] + max_len)
        if final_candidates[-1] != c:
            final_candidates.append(c)

    candidates = final_candidates

    prefix_sums = calculate_prefix_sums(embeddings)
    density_tensor = torch.tensor(densities, dtype=torch.float32)
    density_cumsum = torch.cat((torch.zeros(1), torch.cumsum(density_tensor, dim=0)))

    dp = {0: 0.0}
    backtrack = {0: 0}

    for i in range(1, len(candidates)):
        curr_bnd = candidates[i]
        best_score = float("-inf")
        best_prev = 0

        for j in range(i - 1, -1, -1):
            prev_bnd = candidates[j]
            length = curr_bnd - prev_bnd

            if length > max_len:
                break

            segment_sum = prefix_sums[curr_bnd] - prefix_sums[prev_bnd]
            coherence = torch.norm(segment_sum, p=2).item()

            info_sum = density_cumsum[curr_bnd] - density_cumsum[prev_bnd]
            penalty = penalty_weight * (length**penalty_exponent)

            score = coherence + (density_weight * info_sum.item()) - penalty

            total_score = dp[prev_bnd] + score
            if total_score > best_score:
                best_score = total_score
                best_prev = prev_bnd

        dp[curr_bnd] = best_score
        backtrack[curr_bnd] = best_prev

    segments = []
    curr = N
    while curr > 0:
        prev = backtrack[curr]
        segments.append((prev, curr))
        curr = prev

    segments.reverse()
    return segments


@dataclass(frozen=True)
class SegmentLevel:
    """One abstraction level for multi-level segmentation (G1.10, §2).

    The DP length penalty is the **level knob**: a coarser level uses a larger
    ``max_len`` and a smaller ``penalty_weight``, so :func:`segment_dp` merges the *same*
    valley boundaries into larger segments. ``level`` 0 is the finest (the granularity the
    proposition layer extracts from); higher levels are progressively coarser, for §5.1
    coarse-to-fine pruning. Every level reads the **same** cached token embeddings (embed
    once, derive all granularities — §1/§2); only the penalty / ``max_len`` differ.
    """

    level: int
    max_len: int
    penalty_weight: float
    density_weight: float = 0.5
    penalty_exponent: float = 1.0

    def params(self) -> dict[str, Any]:
        """The segmenter params recorded in the content hash + segment ``Action`` (audit).

        At the default ``penalty_exponent`` (1.0) this is exactly the legacy three-key dict
        ``{max_len, penalty_weight, density_weight}``, so a level-0 span content hash is
        **byte-identical** to the pre-G1.10 value — multi-level is purely additive and forces
        no resegmentation of existing documents on deploy. A non-default exponent adds the
        key, so it cannot drift silently past the resegmentation guard.
        """
        p: dict[str, Any] = {
            "max_len": self.max_len,
            "penalty_weight": self.penalty_weight,
            "density_weight": self.density_weight,
        }
        if self.penalty_exponent != 1.0:
            p["penalty_exponent"] = self.penalty_exponent
        return p


def default_level_policy(
    *, max_len: int = 50, penalty_weight: float = 0.1, density_weight: float = 0.5
) -> list[SegmentLevel]:
    """The default multi-level policy (G1.10): a fine level 0 + one coarse level 1.

    The **count is data** — extend the returned list for more levels (the segmenter takes
    any ``list[SegmentLevel]``); two is the default. The coarse level uses 4× the ``max_len``
    and 1/5 the ``penalty_weight``, so it merges several fine segments into a section-sized
    span. Level 0 carries the caller's base params unchanged, so it stays byte-identical to a
    single-level run.
    """
    return [
        SegmentLevel(0, max_len, penalty_weight, density_weight),
        SegmentLevel(1, max_len * 4, penalty_weight / 5.0, density_weight),
    ]


class SegmentationBackbone:
    def __init__(
        self,
        max_len: int = 50,
        penalty_weight: float = 0.1,
        density_weight: float = 0.5,
        *,
        levels: list[SegmentLevel] | None = None,
    ):
        self.max_len = max_len
        self.penalty_weight = penalty_weight
        self.density_weight = density_weight
        # ``levels`` is the configurable level policy (G1.10). Omitted ⇒ a single finest level
        # from the scalar params — byte-identical to the pre-G1.10 backbone, which is what the
        # existing single-level callers/tests get. Production ingest passes
        # ``default_level_policy(...)`` (2 levels). The level *count* lives in the list, not in
        # code, so adding granularities is a config change, not an edit.
        self.levels: list[SegmentLevel] = (
            levels
            if levels is not None
            else [SegmentLevel(0, max_len, penalty_weight, density_weight)]
        )

    def _embeddings_and_signal(
        self, sentences: list[dict[str, Any]], context: _PoolingContext
    ) -> tuple[list[list[float]], list[float], list[int]] | None:
        """Pool every sentence + derive the boundary signal, **once**, for all levels.

        Returns ``(embeddings, densities, valleys)`` — the level-independent inputs to
        :func:`segment_dp`; only the penalty / ``max_len`` vary per level, so this is computed
        a single time and reused (no re-embedding, no re-pooling per level). Returns ``None``
        for the fully degenerate input where **no** sentence overlaps a token (so there is no
        signal to segment on); the caller emits one covering span at the finest level.

        Windowed embedding (G1.13 slice 2) is transparent here: each sentence pools from the
        macro-window where it sits furthest from a window edge, so adjacent sentences select the
        same interior window and their cosine compares one consistent context.

        ``pool_span`` returns ``None`` for a sentence overlapping no token (review R3);
        ``split_sentences`` already drops whitespace-only runs, so this is effectively
        unreachable, but to stay total a zero vector is substituted for the *internal* adjacency
        math only (it never persists — a boundary signal, not an index row).
        """
        pooled = [context.pool_span(s["start_char"], s["end_char"]) for s in sentences]
        hidden = next((len(e) for e in pooled if e is not None), 0)
        if hidden == 0:
            return None
        embeddings = [e if e is not None else [0.0] * hidden for e in pooled]
        densities = [calculate_information_density(s["text"]) for s in sentences]
        sims = calculate_adjacent_similarities(embeddings)
        smoothed = smooth_similarities(sims, window_size=1)
        valleys = find_valleys(smoothed, k=1.0)
        return embeddings, densities, valleys

    def _char_spans_for_level(
        self,
        sentences: list[dict[str, Any]],
        embeddings: list[list[float]],
        densities: list[float],
        valleys: list[int],
        lvl: SegmentLevel,
    ) -> list[tuple[int, int]]:
        segment_indices = segment_dp(
            embeddings=embeddings,
            valleys=valleys,
            densities=densities,
            max_len=lvl.max_len,
            penalty_weight=lvl.penalty_weight,
            penalty_exponent=lvl.penalty_exponent,
            density_weight=lvl.density_weight,
        )
        return [
            (sentences[start_idx]["start_char"], sentences[end_idx - 1]["end_char"])
            for start_idx, end_idx in segment_indices
        ]

    def segment_document(
        self, sentences: list[dict[str, Any]], context: _PoolingContext
    ) -> list[tuple[int, int]]:
        """Single-level segmentation at the finest scalar params (the level-0 granularity).

        sentences: list of dicts with 'text', 'start_char', 'end_char'. Retained as the
        backbone's original contract (used by ``scripts/illustrate.py`` and the unit tests);
        :meth:`segment_document_levels` is the multi-level entry the ingest path uses.
        """
        if not sentences:
            return []
        signal = self._embeddings_and_signal(sentences, context)
        if signal is None:
            return [(sentences[0]["start_char"], sentences[-1]["end_char"])]
        embeddings, densities, valleys = signal
        return self._char_spans_for_level(
            sentences,
            embeddings,
            densities,
            valleys,
            SegmentLevel(0, self.max_len, self.penalty_weight, self.density_weight),
        )

    def segment_document_levels(
        self, sentences: list[dict[str, Any]], context: _PoolingContext
    ) -> list[tuple[SegmentLevel, list[tuple[int, int]]]]:
        """Segment at **every** configured level from one cached embedding pass (G1.10).

        Returns ``[(level, char_spans), ...]`` in ascending level order. The pooled token
        embeddings, the density signal, and the valley candidates are computed once and shared
        across levels (embed once — §1/§2); only the length penalty / ``max_len`` differ, so a
        coarser level merges the same boundaries into larger spans. The levels are *independent
        granularities*, not a strict containment tree — RAPTOR-style nesting with parent links
        is Part B / Phase 2 ``PART_OF`` (G2.5); this part stores the offset ranges per level.
        """
        if not sentences:
            return [(lvl, []) for lvl in sorted(self.levels, key=lambda x: x.level)]
        signal = self._embeddings_and_signal(sentences, context)
        if signal is None:
            # No token-bearing sentence: there is no signal to segment on at any granularity,
            # so emit one covering span at the finest level only (coarse levels would be
            # meaningless). Matches single-level segment_document's degenerate behaviour.
            finest = min(self.levels, key=lambda x: x.level)
            return [(finest, [(sentences[0]["start_char"], sentences[-1]["end_char"])])]
        embeddings, densities, valleys = signal
        return [
            (lvl, self._char_spans_for_level(sentences, embeddings, densities, valleys, lvl))
            for lvl in sorted(self.levels, key=lambda x: x.level)
        ]
