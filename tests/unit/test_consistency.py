"""Unit tests for self-consistency clustering + agreement (G1.3).

Pure: hand-built toy vectors, no torch / DB / LLM. Covers the contract the multi-sample
extraction relies on — deterministic order-independent clustering, agreement as the distinct-
sample fraction (never inflated by within-sample duplicates), medoid canonical selection with a
deterministic tie-break, and the N=1 / all-unique degenerate cases.
"""

import pytest

from iknos.core.consistency import (
    DEFAULT_AGREEMENT_THRESHOLD,
    Candidate,
    agreement_of,
    canonical_of,
    cluster_candidates,
)
from iknos.types.epistemic import Attribution, EpistemicClass, Modality, Polarity


def _cand(
    text: str,
    embedding: list[float],
    *,
    sample_index: int,
    position: int = 0,
    modality: Modality = Modality.CATEGORICAL,
) -> Candidate:
    return Candidate(
        text=text,
        polarity=Polarity.ASSERTED,
        modality=modality,
        attribution=Attribution.DOCUMENT,
        scope="",
        epistemic_class=EpistemicClass.OBSERVATION,
        embedding=embedding,
        sample_index=sample_index,
        position=position,
    )


# --- clustering ---


def test_identical_extractions_across_samples_form_one_cluster() -> None:
    cands = [_cand("the bearing failed", [1.0, 0.0], sample_index=i) for i in range(3)]
    clusters = cluster_candidates(cands)
    assert len(clusters) == 1
    assert {c.sample_index for c in clusters[0]} == {0, 1, 2}


def test_orthogonal_extractions_form_separate_clusters() -> None:
    cands = [
        _cand("a", [1.0, 0.0], sample_index=0),
        _cand("b", [0.0, 1.0], sample_index=1),
    ]
    clusters = cluster_candidates(cands)
    assert len(clusters) == 2


def test_clustering_is_order_independent() -> None:
    # Same candidates, two input orders → same partition (as text sets).
    a = _cand("a", [1.0, 0.0], sample_index=0, position=0)
    b = _cand("a2", [0.999, 0.044], sample_index=1, position=0)  # ~same as a
    c = _cand("c", [0.0, 1.0], sample_index=2, position=0)
    p1 = cluster_candidates([a, b, c])
    p2 = cluster_candidates([c, b, a])

    def as_sets(part: list[list[Candidate]]) -> list[tuple[str, ...]]:
        return sorted(tuple(sorted(m.text for m in cl)) for cl in part)

    assert as_sets(p1) == as_sets(p2)


def test_threshold_boundary_controls_merging() -> None:
    near = [_cand("x", [1.0, 0.0], sample_index=0), _cand("y", [0.9, 0.4359], sample_index=1)]
    # cos ≈ 0.9; a 0.95 threshold keeps them apart, a 0.85 threshold merges them.
    assert len(cluster_candidates(near, threshold=0.95)) == 2
    assert len(cluster_candidates(near, threshold=0.85)) == 1


def test_no_chaining_through_a_bridge() -> None:
    # A ~ B and B ~ C, but A ⟂ C. Greedy-against-representative must not transitively merge
    # all three: A opens a cluster (rep=A), B joins A, C is compared to rep A only (not B) → new.
    a = _cand("a", [1.0, 0.0], sample_index=0)
    b = _cand("b", [0.8, 0.6], sample_index=1)  # cos(a,b)=0.8
    c = _cand("c", [0.0, 1.0], sample_index=2)  # cos(a,c)=0
    clusters = cluster_candidates([a, b, c], threshold=0.75)
    assert sorted(len(cl) for cl in clusters) == [1, 2]


# --- agreement ---


def test_agreement_is_distinct_sample_fraction() -> None:
    cluster = [_cand("x", [1.0, 0.0], sample_index=i) for i in (0, 1)]
    assert agreement_of(cluster, n_samples=3) == pytest.approx(2 / 3)


def test_within_sample_duplicates_do_not_inflate_agreement() -> None:
    # Two near-duplicates from the SAME sample count once, not twice.
    cluster = [
        _cand("x", [1.0, 0.0], sample_index=0, position=0),
        _cand("x again", [1.0, 0.0], sample_index=0, position=1),
    ]
    assert agreement_of(cluster, n_samples=3) == pytest.approx(1 / 3)


def test_agreement_full_and_clamped() -> None:
    cluster = [_cand("x", [1.0, 0.0], sample_index=i) for i in range(3)]
    assert agreement_of(cluster, n_samples=3) == 1.0


def test_agreement_rejects_zero_samples() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        agreement_of([_cand("x", [1.0, 0.0], sample_index=0)], n_samples=0)


# --- canonical (medoid) ---


def test_canonical_singleton_returns_member() -> None:
    only = _cand("x", [1.0, 0.0], sample_index=0)
    assert canonical_of([only]) is only


def test_canonical_picks_central_medoid() -> None:
    # Two near-identical phrasings + one outlier; the medoid is one of the central pair.
    central1 = _cand("c1", [1.0, 0.0], sample_index=0)
    central2 = _cand("c2", [0.9988, 0.0497], sample_index=1)  # cos≈0.999 to central1
    outlier = _cand("out", [0.0, 1.0], sample_index=2)
    medoid = canonical_of([outlier, central1, central2])
    assert medoid.text in {"c1", "c2"}


def test_canonical_tie_break_is_smallest_sample_position() -> None:
    # Symmetric pair → equal mean similarity; deterministic winner is (sample_index, position) min.
    a = _cand("a", [1.0, 0.0], sample_index=1, position=0)
    b = _cand("b", [1.0, 0.0], sample_index=0, position=5)
    assert canonical_of([a, b]).text == "b"  # sample 0 < sample 1


def test_canonical_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty cluster"):
        canonical_of([])


def test_default_threshold_is_exposed() -> None:
    assert 0.0 < DEFAULT_AGREEMENT_THRESHOLD <= 1.0
