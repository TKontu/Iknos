"""G3.7 — unit tests for the pure `SAME_AS`-component aggregation (DB-free).

The DB path of :class:`ComponentReasoner` (the merge/split belief revisions) is exercised by
the integration test; here we pin the aggregation algebra (:func:`aggregate_components`,
:func:`canonical_map`) — additive Layer A support, `⊕`-idempotent Layer B confidence,
foundedness gating, no intra-node double-count — with hand-built maps.
"""

from iknos.core.component_aggregate import aggregate_components, canonical_map
from iknos.core.confidence import GODEL


def test_unmerged_singletons_aggregate_to_themselves() -> None:
    # Two distinct entities, each in one fact -> each is its own canonical.
    agg = aggregate_components(
        involves={"F1": frozenset({"E1"}), "F2": frozenset({"E2"})},
        canonical_of={},  # no SAME_AS -> singletons
        support_count={"F1": 1, "F2": 1},
        confidence={"F1": 0.8, "F2": 0.6},
    )
    assert set(agg) == {"E1", "E2"}
    assert agg["E1"].support_count == 1
    assert agg["E1"].confidence == 0.8
    assert agg["E1"].members == frozenset({"E1"})


def test_merge_accrues_support_additively_and_confidence_by_max() -> None:
    # E1 and E2 are SAME_AS (canonical E1). Facts F1 (E1) and F2 (E2) now accrue to E1:
    # support sums to 2 (Layer A additive); confidence is the best, max(0.7, 0.9) = 0.9.
    agg = aggregate_components(
        involves={"F1": frozenset({"E1"}), "F2": frozenset({"E2"})},
        canonical_of=canonical_map([frozenset({"E1", "E2"})]),
        support_count={"F1": 1, "F2": 1},
        confidence={"F1": 0.7, "F2": 0.9},
        semiring=GODEL,
    )
    assert set(agg) == {"E1"}  # one canonical component
    ev = agg["E1"]
    assert ev.members == frozenset({"E1", "E2"})
    assert ev.nodes == frozenset({"F1", "F2"})
    assert ev.support_count == 2  # additive
    assert ev.confidence == 0.9  # ⊕ = max (best evidence)


def test_unsupported_node_contributes_no_evidence() -> None:
    # F2 has support_count 0 (Layer A unfounded) -> excluded from the aggregate (§12 gate).
    agg = aggregate_components(
        involves={"F1": frozenset({"E1"}), "F2": frozenset({"E1"})},
        canonical_of={},
        support_count={"F1": 1, "F2": 0},
        confidence={"F1": 0.5},
    )
    assert agg["E1"].nodes == frozenset({"F1"})
    assert agg["E1"].support_count == 1


def test_node_mentioning_merged_members_accrues_once() -> None:
    # A single fact mentions both E1 and E2, which are merged (canonical E1). It must accrue
    # ONCE to E1 (nodes is a set), not double its support.
    agg = aggregate_components(
        involves={"F1": frozenset({"E1", "E2"})},
        canonical_of=canonical_map([frozenset({"E1", "E2"})]),
        support_count={"F1": 3},
        confidence={"F1": 0.5},
    )
    assert set(agg) == {"E1"}
    assert agg["E1"].support_count == 3  # not 6
    assert agg["E1"].nodes == frozenset({"F1"})


def test_confidence_idempotent_across_overlapping_evidence() -> None:
    # Re-presenting the same confidence (two facts, equal conf) does not inflate ⊕=max.
    agg = aggregate_components(
        involves={"F1": frozenset({"E1"}), "F2": frozenset({"E1"})},
        canonical_of={},
        support_count={"F1": 1, "F2": 1},
        confidence={"F1": 0.6, "F2": 0.6},
    )
    assert agg["E1"].confidence == 0.6  # idempotent
    assert agg["E1"].support_count == 2  # but support still counts both


def test_canonical_map_uses_lexicographic_min_representative() -> None:
    m = canonical_map([frozenset({"b", "a", "c"}), frozenset({"z", "y"})])
    assert m == {"a": "a", "b": "a", "c": "a", "y": "y", "z": "y"}


def test_split_back_to_singletons_separates_evidence() -> None:
    # After a split (no SAME_AS), the same facts aggregate to two separate entities again —
    # the recoverability §5.2 promises, here at the pure-algebra level.
    involves = {"F1": frozenset({"E1"}), "F2": frozenset({"E2"})}
    sc = {"F1": 1, "F2": 1}
    conf = {"F1": 0.7, "F2": 0.9}
    merged = aggregate_components(
        involves=involves,
        canonical_of=canonical_map([frozenset({"E1", "E2"})]),
        support_count=sc,
        confidence=conf,
    )
    split = aggregate_components(
        involves=involves, canonical_of={}, support_count=sc, confidence=conf
    )
    assert set(merged) == {"E1"} and merged["E1"].support_count == 2
    assert set(split) == {"E1", "E2"}
    assert split["E1"].support_count == 1 and split["E2"].support_count == 1
