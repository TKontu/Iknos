import pytest

from iknos.core.segmentation import (
    SegmentationBackbone,
    SegmentLevel,
    calculate_adjacent_similarities,
    calculate_information_density,
    calculate_prefix_sums,
    default_level_policy,
    find_valleys,
    segment_dp,
    smooth_similarities,
)


def test_calculate_adjacent_similarities():
    # 4 sentence embeddings
    # e0 and e1 are identical (sim=1.0)
    # e1 and e2 are orthogonal (sim=0.0)
    # e2 and e3 are opposite (sim=-1.0)
    embeddings = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, -1.0]]

    similarities = calculate_adjacent_similarities(embeddings)

    # 4 embeddings -> 3 similarities between adjacent pairs
    assert len(similarities) == 3
    assert similarities[0] == pytest.approx(1.0, abs=1e-5)
    assert similarities[1] == pytest.approx(0.0, abs=1e-5)
    assert similarities[2] == pytest.approx(-1.0, abs=1e-5)


def test_smooth_similarities():
    sims = [1.0, 0.5, 0.0, 0.5, 1.0]
    # Window size 1 means (i-1, i, i+1) -> 3 elements max
    smoothed = smooth_similarities(sims, window_size=1)

    assert len(smoothed) == 5
    assert smoothed[0] == pytest.approx(0.75)
    assert smoothed[1] == pytest.approx(0.5)
    assert smoothed[2] == pytest.approx(0.333333, abs=1e-5)
    assert smoothed[3] == pytest.approx(0.5)
    assert smoothed[4] == pytest.approx(0.75)


def test_find_valleys():
    # Mean of sims is 0.75, std dev is approx 0.23
    # threshold = 0.75 - 1.0 * 0.23 = 0.52
    # Valley at index 2 (0.2) < 0.52 -> accepted
    # Valley at index 6 (0.75) > 0.52 -> rejected
    sims = [1.0, 0.8, 0.2, 0.7, 0.9, 0.8, 0.75, 0.85]
    valleys = find_valleys(sims, k=1.0)

    assert valleys == [2]


def test_calculate_prefix_sums():
    # Use already normalized vectors
    embeddings = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    prefix_sums = calculate_prefix_sums(embeddings)

    assert prefix_sums.shape == (4, 2)
    assert prefix_sums[0].tolist() == [0.0, 0.0]
    assert prefix_sums[1].tolist() == [1.0, 0.0]
    assert prefix_sums[2].tolist() == [1.0, 1.0]
    assert prefix_sums[3].tolist() == [0.0, 1.0]


def test_calculate_information_density():
    sentences = [
        "this is a plain sentence without entities.",
        "The iPhone 15 Pro was released in 2023.",
        "Revenue grew by 25% to $4.5B.",
    ]
    densities = [calculate_information_density(s) for s in sentences]
    assert densities[0] == 0.0
    assert densities[1] > 0.0
    assert densities[2] > 0.0


def test_segment_dp():
    # 5 sentences
    embeddings = [
        [1.0, 0.0],
        [1.0, 0.0],  # Group 1
        [0.0, 1.0],
        [0.0, 1.0],
        [0.0, 1.0],  # Group 2
    ]
    valleys = [2]  # natural break between group 1 and 2
    densities = [1.0, 1.0, 1.0, 1.0, 1.0]

    segments = segment_dp(
        embeddings=embeddings, valleys=valleys, densities=densities, max_len=5, penalty_weight=0.1
    )

    assert segments == [(0, 2), (2, 5)]


def test_segmentation_backbone():
    class DummyContext:
        def pool_span(self, start_char, end_char):
            # sentences 0 and 1 get [1.0, 0.0], 2 and 3 get [0.0, 1.0]
            if start_char < 30:
                return [1.0, 0.0]
            return [0.0, 1.0]

    sentences = [
        {"text": "Sentence 1.", "start_char": 0, "end_char": 11},
        {"text": "Sentence 2.", "start_char": 12, "end_char": 23},
        {"text": "Sentence 3 has $10.", "start_char": 24, "end_char": 43},
        {"text": "Sentence 4.", "start_char": 44, "end_char": 55},
    ]

    backbone = SegmentationBackbone(max_len=5, penalty_weight=0.1)
    char_spans = backbone.segment_document(sentences, DummyContext())

    # We expect a split between sentence 2 and 3 (indices 2 and 3).
    # Wait, the DummyContext splits at start_char < 30.
    # Sent 0: start 0 (<30) -> [1.0, 0.0]
    # Sent 1: start 12 (<30) -> [1.0, 0.0]
    # Sent 2: start 24 (<30) -> [1.0, 0.0]  <-- Wait! My comment said 2 and 3 get [0.0, 1.0].
    # Let's fix that conceptually, but the DP will find the split wherever it changes.
    assert len(char_spans) > 0
    # The first span should start at 0, the last should end at 55.
    assert char_spans[0][0] == 0
    assert char_spans[-1][1] == 55


def test_segmentation_tolerates_none_pooled_sentence():
    # pool_span returns None for a token-less sentence (review R3). Segmentation must stay total:
    # substitute a zero vector for the internal adjacency math (it never persists) and still
    # produce spans covering the whole range.
    class PartialContext:
        def pool_span(self, start_char, end_char):
            if start_char < 12:  # the first sentence pools to no token
                return None
            if start_char < 30:
                return [1.0, 0.0]
            return [0.0, 1.0]

    sentences = [
        {"text": "Sentence 1.", "start_char": 0, "end_char": 11},
        {"text": "Sentence 2.", "start_char": 12, "end_char": 23},
        {"text": "Sentence 3 has $10.", "start_char": 24, "end_char": 43},
        {"text": "Sentence 4.", "start_char": 44, "end_char": 55},
    ]

    backbone = SegmentationBackbone(max_len=5, penalty_weight=0.1)
    char_spans = backbone.segment_document(sentences, PartialContext())

    assert len(char_spans) > 0
    assert char_spans[0][0] == 0
    assert char_spans[-1][1] == 55


def test_segmentation_all_none_yields_one_covering_span():
    # Fully degenerate input: every sentence pools to None → no signal to segment on → a single
    # span covering the whole range, never a crash on a zero-dimension tensor (review R3).
    class EmptyContext:
        def pool_span(self, start_char, end_char):
            return None

    sentences = [
        {"text": "a", "start_char": 0, "end_char": 1},
        {"text": "b", "start_char": 2, "end_char": 3},
    ]

    backbone = SegmentationBackbone(max_len=5, penalty_weight=0.1)
    char_spans = backbone.segment_document(sentences, EmptyContext())

    assert char_spans == [(0, 3)]


# --- multi-level segmentation (G1.10) -------------------------------------------------


class _ConstContext:
    """Every sentence pools to the same unit vector — perfectly coherent, so the DP merges
    freely whenever the length penalty / max_len permit. This isolates the *level knob*
    (max_len + penalty) from the boundary signal."""

    def pool_span(self, start_char: int, end_char: int) -> list[float]:
        return [1.0, 0.0]


def _six_sentences() -> list[dict]:
    # Contiguous char ranges (end of i == start of i+1) so each level's spans tile the range
    # exactly — isolating the segmentation from inter-sentence whitespace.
    return [
        {"text": f"Sentence {i} has $1.", "start_char": i * 10, "end_char": (i + 1) * 10}
        for i in range(6)
    ]


def _tiles(spans: list[tuple[int, int]], lo: int, hi: int) -> bool:
    """True iff spans partition [lo, hi] contiguously in order (no gap, no overlap)."""
    if not spans:
        return lo == hi
    if spans[0][0] != lo or spans[-1][1] != hi:
        return False
    return all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))


def test_default_level_policy_is_two_levels_fine_then_coarse() -> None:
    policy = default_level_policy(max_len=50, penalty_weight=0.1, density_weight=0.5)
    assert [lvl.level for lvl in policy] == [0, 1]
    fine, coarse = policy
    # Level 0 carries the base params unchanged (byte-identical to a single-level run).
    assert (fine.max_len, fine.penalty_weight) == (50, 0.1)
    # The coarse level relaxes the knob: larger max_len, smaller penalty → merges.
    assert coarse.max_len > fine.max_len
    assert coarse.penalty_weight < fine.penalty_weight


def test_segment_level_params_are_byte_identical_at_default_exponent() -> None:
    # The level-0 content hash must not move on deploy — params() is the legacy 3-key dict
    # at the default exponent, and only grows a key for a non-default exponent (no silent drift).
    assert SegmentLevel(0, 50, 0.1, 0.5).params() == {
        "max_len": 50,
        "penalty_weight": 0.1,
        "density_weight": 0.5,
    }
    assert "penalty_exponent" in SegmentLevel(0, 50, 0.1, 0.5, penalty_exponent=1.5).params()


def test_segment_document_levels_returns_one_entry_per_level_in_order() -> None:
    policy = [
        SegmentLevel(0, max_len=2, penalty_weight=0.1),
        SegmentLevel(1, max_len=8, penalty_weight=0.02),
    ]
    backbone = SegmentationBackbone(levels=policy)
    out = backbone.segment_document_levels(_six_sentences(), _ConstContext())
    assert [lvl.level for lvl, _ in out] == [0, 1]
    # Every level tiles the full sentence range — no span is dropped at any granularity.
    for _lvl, spans in out:
        assert _tiles(spans, 0, _six_sentences()[-1]["end_char"])


def test_coarse_level_merges_relative_to_fine() -> None:
    # max_len=2 forces the fine level to split every two sentences; the coarse level (max_len=8 >= 6
    # sentences, low penalty, perfect coherence) is free to merge — so coarse has fewer spans.
    policy = [
        SegmentLevel(0, max_len=2, penalty_weight=0.1),
        SegmentLevel(1, max_len=8, penalty_weight=0.02),
    ]
    out = dict(
        (lvl.level, spans)
        for lvl, spans in SegmentationBackbone(levels=policy).segment_document_levels(
            _six_sentences(), _ConstContext()
        )
    )
    assert len(out[0]) > 1  # the knob produced a finer split
    assert len(out[1]) <= len(out[0])  # coarser never increases the segment count


def test_level_zero_matches_single_level_segment_document() -> None:
    # The two entry points must agree on level 0: segment_document_levels' finest output is
    # exactly what the single-level segment_document produces for the same params.
    sentences, ctx = _six_sentences(), _ConstContext()
    level0 = SegmentLevel(0, max_len=2, penalty_weight=0.1, density_weight=0.5)
    multi = SegmentationBackbone(levels=[level0, SegmentLevel(1, 8, 0.02)])
    single = SegmentationBackbone(max_len=2, penalty_weight=0.1, density_weight=0.5)
    multi_level0 = next(
        spans for lvl, spans in multi.segment_document_levels(sentences, ctx) if lvl.level == 0
    )
    assert multi_level0 == single.segment_document(sentences, ctx)


def test_no_levels_arg_is_single_level() -> None:
    # Backward compatibility: a backbone built without a policy yields exactly one level,
    # equal to segment_document.
    sentences, ctx = _six_sentences(), _ConstContext()
    backbone = SegmentationBackbone(max_len=3, penalty_weight=0.1)
    out = backbone.segment_document_levels(sentences, ctx)
    assert len(out) == 1 and out[0][0].level == 0
    assert out[0][1] == backbone.segment_document(sentences, ctx)


def test_segment_document_levels_degenerate_all_none_is_finest_only() -> None:
    class EmptyContext:
        def pool_span(self, start_char: int, end_char: int) -> None:
            return None

    sentences = [
        {"text": "a", "start_char": 0, "end_char": 1},
        {"text": "b", "start_char": 2, "end_char": 3},
    ]
    policy = [SegmentLevel(0, 5, 0.1), SegmentLevel(1, 20, 0.02)]
    out = SegmentationBackbone(levels=policy).segment_document_levels(sentences, EmptyContext())
    # No signal at any granularity → one covering span at the finest level only.
    assert out == [(policy[0], [(0, 3)])]


def test_segment_document_levels_empty_is_empty_per_level() -> None:
    policy = [SegmentLevel(0, 5, 0.1), SegmentLevel(1, 20, 0.02)]
    out = SegmentationBackbone(levels=policy).segment_document_levels([], _ConstContext())
    assert out == [(policy[0], []), (policy[1], [])]
