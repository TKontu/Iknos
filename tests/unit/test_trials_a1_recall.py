"""Hand-computed fixtures for the A1 split-recall scorer (``iknos.trials.a1_recall``).

Each case pins the *definition* of supporter / refuter / dissimilar-refuter recall and the
adjudication-cost read against a value computed by hand in the comment. The dangerous axis
(refuter recall) and its binding subset (dissimilar refuters) get dedicated cases, mirroring the
§5.1 reason A1 exists.
"""

from __future__ import annotations

import pytest

from iknos.trials.a1_recall import (
    EdgeSign,
    GoldEdge,
    RecallResult,
    project_to_gold,
    recall_curve,
    score_recall,
)

# A small gold set in the shape the gate manifest yields: 2 supporters, 2 refuters (both
# dissimilar) — the dissimilar subset is the binding constraint.
GOLD = [
    GoldEdge("s1", "H1", EdgeSign.SUPPORTS),
    GoldEdge("s2", "H2", EdgeSign.SUPPORTS),
    GoldEdge("r1", "H3", EdgeSign.REFUTES, dissimilar=True),
    GoldEdge("r2", "H4", EdgeSign.REFUTES, dissimilar=True),
]


def test_perfect_recall_when_all_gold_retrieved() -> None:
    retrieved = [("s1", "H1"), ("s2", "H2"), ("r1", "H3"), ("r2", "H4"), ("x", "H1")]
    r = score_recall(retrieved, GOLD)
    assert r.supporter_recall == pytest.approx(1.0)
    assert r.refuter_recall == pytest.approx(1.0)
    assert r.dissimilar_refuter_recall == pytest.approx(1.0)
    assert r.n_candidates == 5  # whole deduped pool at the default budget
    assert (r.n_supporters, r.n_refuters, r.n_dissimilar_refuters) == (2, 2, 2)


def test_split_recall_support_found_refuters_missed() -> None:
    # The §5.1 failure mode: embedding-similar supporters recalled, dissimilar refuters missed.
    retrieved = [("s1", "H1"), ("s2", "H2"), ("noise", "H1")]
    r = score_recall(retrieved, GOLD)
    assert r.supporter_recall == pytest.approx(1.0)  # both supporters in
    assert r.refuter_recall == pytest.approx(0.0)  # neither refuter in
    assert r.dissimilar_refuter_recall == pytest.approx(0.0)


def test_budget_cutoff_excludes_later_candidates() -> None:
    # Rank order with refuters ranked low; budget 2 sees only the supporters.
    retrieved = [("s1", "H1"), ("s2", "H2"), ("r1", "H3"), ("r2", "H4")]
    r = score_recall(retrieved, GOLD, budget=2)
    assert r.n_candidates == 2
    assert r.supporter_recall == pytest.approx(1.0)
    assert r.refuter_recall == pytest.approx(0.0)  # refuters past the budget
    # Widen the budget: the refuters come into reach.
    r4 = score_recall(retrieved, GOLD, budget=4)
    assert r4.refuter_recall == pytest.approx(1.0)


def test_partial_refuter_recall() -> None:
    retrieved = [("r1", "H3"), ("s1", "H1")]  # one of two refuters
    r = score_recall(retrieved, GOLD)
    assert r.refuter_recall == pytest.approx(0.5)
    assert r.dissimilar_refuter_recall == pytest.approx(0.5)
    assert r.supporter_recall == pytest.approx(0.5)


def test_dissimilar_subset_is_narrower_than_all_refuters() -> None:
    # A refuter that is NOT dissimilar: it counts for refuter recall but not the dissimilar subset.
    gold = [
        GoldEdge("r1", "H3", EdgeSign.REFUTES, dissimilar=True),
        GoldEdge("r2", "H4", EdgeSign.REFUTES, dissimilar=False),
    ]
    retrieved = [("r2", "H4")]  # only the similar refuter retrieved
    r = score_recall(retrieved, gold)
    assert r.n_refuters == 2
    assert r.n_dissimilar_refuters == 1
    assert r.refuter_recall == pytest.approx(0.5)  # 1 of 2 refuters
    assert r.dissimilar_refuter_recall == pytest.approx(0.0)  # the dissimilar one was missed


def test_duplicate_candidate_cannot_inflate_recall() -> None:
    retrieved = [("r1", "H3"), ("r1", "H3"), ("r1", "H3")]  # same pair repeated
    r = score_recall(retrieved, GOLD)
    assert r.refuter_recall == pytest.approx(0.5)  # still only one of two refuters
    assert r.n_candidates == 1  # deduped cost


def test_empty_subset_recall_is_none_not_zero() -> None:
    gold = [GoldEdge("s1", "H1", EdgeSign.SUPPORTS)]  # no refuters at all
    r = score_recall([("s1", "H1")], gold)
    assert r.supporter_recall == pytest.approx(1.0)
    assert r.refuter_recall is None  # undefined, not a misleading 0.0
    assert r.dissimilar_refuter_recall is None
    assert r.n_refuters == 0


def test_project_to_gold_keeps_only_fully_mapped_pairs() -> None:
    # Node-space candidates; only nodes that map on BOTH endpoints become gold-space edges.
    node_pairs = [("n_r1", "n_H3"), ("n_noise", "n_H3"), ("n_s1", "n_unknown_hyp")]
    ev_map = {"n_r1": "r1", "n_s1": "s1"}  # n_noise unmapped
    hyp_map = {"n_H3": "H3"}  # n_unknown_hyp unmapped
    assert project_to_gold(node_pairs, ev_map, hyp_map) == [("r1", "H3")]


def test_project_to_gold_preserves_order_for_ranked_pools() -> None:
    node_pairs = [("n_r2", "n_H4"), ("n_r1", "n_H3")]
    ev_map = {"n_r1": "r1", "n_r2": "r2"}
    hyp_map = {"n_H3": "H3", "n_H4": "H4"}
    assert project_to_gold(node_pairs, ev_map, hyp_map) == [("r2", "H4"), ("r1", "H3")]


def test_project_then_score_recovers_recall() -> None:
    # End-to-end: project a node-space pool, then score it — the harness path the runner uses.
    node_pairs = [("n_s1", "n_H1"), ("n_r1", "n_H3")]
    ev_map = {"n_s1": "s1", "n_r1": "r1"}
    hyp_map = {"n_H1": "H1", "n_H3": "H3"}
    projected = project_to_gold(node_pairs, ev_map, hyp_map)
    r = score_recall(projected, GOLD)
    assert r.supporter_recall == pytest.approx(0.5)  # 1 of 2 supporters
    assert r.refuter_recall == pytest.approx(0.5)  # 1 of 2 refuters


def test_negative_budget_raises() -> None:
    with pytest.raises(ValueError, match="budget must be non-negative"):
        score_recall([("s1", "H1")], GOLD, budget=-1)


def test_recall_curve_is_monotone_nondecreasing_in_budget() -> None:
    retrieved = [("s1", "H1"), ("r1", "H3"), ("s2", "H2"), ("r2", "H4")]
    curve = recall_curve(retrieved, GOLD, [1, 2, 3, 4])
    assert all(isinstance(r, RecallResult) for r in curve)
    refuter_recalls = [r.refuter_recall for r in curve]
    assert refuter_recalls == [pytest.approx(0.0), 0.5, 0.5, pytest.approx(1.0)]
    # cost grows with budget
    assert [r.n_candidates for r in curve] == [1, 2, 3, 4]
