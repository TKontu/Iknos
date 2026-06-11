"""G4.2 unit tests — candidate generation (architecture §5.1), DB-free.

The pure funnel core + the structural-entity prior, hand-built rows (no DB). Headline is the
**dissimilar-refuter decision fixture**: the funnel unions cheap generators (recall-first) so a
refuter the structural stage catches but the embedding stage misses survives — intersecting
would drop it, re-introducing the support-bias §5.1 forbids.
"""

from iknos.core.candidates import (
    DEFAULT_STRATEGY,
    Candidate,
    CandidateGenerationAdapter,
    CandidatePool,
    CandidateSource,
    FunnelStrategy,
    InvolvesRow,
    funnel,
    structural_entity_candidates,
)


def _involves(*specs: tuple[str, str]) -> list[InvolvesRow]:
    """``(node, entity)`` rows — role defaulted (the structural prior is role-agnostic)."""
    return [InvolvesRow(node=n, entity=e) for n, e in specs]


def _structural(evidence: str, hypothesis: str, *entities: str) -> Candidate:
    return Candidate(
        evidence=evidence,
        hypothesis=hypothesis,
        sources=frozenset({CandidateSource.STRUCTURAL_ENTITY}),
        shared_entities=frozenset(entities),
    )


# --- the decision fixture (§5.1): union over intersection for the cheap stages -----------------


def test_decision_fixture_union_keeps_the_dissimilar_refuter_intersect_drops_it() -> None:
    """A refuter shares the hypothesis's entity (structural-positive) but is embedding-dissimilar
    (the embedding stage never proposes it). Under the recall-first UNION default it survives;
    under INTERSECT it is dropped — exactly the dissimilar-refuter support-bias §5.1 forbids."""
    # Stage 1 (structural) proposes the refuter pair; the stand-in stage 2 (embedding) does not —
    # it only proposes a semantically-similar supporter.
    structural = [_structural("refuter", "H", "shared-actor")]
    embedding = [
        Candidate("supporter", "H", frozenset({CandidateSource.STRUCTURAL_ENTITY})),
    ]

    union = funnel(structural, embedding, strategy=FunnelStrategy.UNION)
    intersect = funnel(structural, embedding, strategy=FunnelStrategy.INTERSECT)

    union_pairs = {c.key for c in union.candidates}
    intersect_pairs = {c.key for c in intersect.candidates}

    # UNION keeps the dissimilar refuter; INTERSECT loses it (and keeps nothing — no pair was
    # proposed by both stages).
    assert ("refuter", "H") in union_pairs
    assert ("refuter", "H") not in intersect_pairs
    assert DEFAULT_STRATEGY is FunnelStrategy.UNION


def test_intersect_keeps_only_pairs_every_generator_proposed() -> None:
    a = [_structural("e1", "H"), _structural("e2", "H")]
    b = [_structural("e2", "H"), _structural("e3", "H")]
    pool = funnel(a, b, strategy=FunnelStrategy.INTERSECT)
    assert {c.key for c in pool.candidates} == {("e2", "H")}


# --- the structural-entity prior (stage 1) -----------------------------------------------------


def test_shared_entity_pairs_evidence_with_hypothesis() -> None:
    pool = funnel(
        structural_entity_candidates(
            hypotheses=["H"],
            evidence=["F"],
            involves=_involves(("H", "actor1"), ("F", "actor1")),
        )
    )
    assert [c.key for c in pool.candidates] == [("F", "H")]
    (c,) = pool.candidates
    assert c.sources == frozenset({CandidateSource.STRUCTURAL_ENTITY})
    assert c.shared_entities == frozenset({"actor1"})


def test_no_shared_entity_yields_no_candidate() -> None:
    cands = structural_entity_candidates(
        hypotheses=["H"],
        evidence=["F"],
        involves=_involves(("H", "actor1"), ("F", "actor2")),
    )
    assert cands == []


def test_multiple_shared_entities_collapse_to_one_candidate() -> None:
    cands = structural_entity_candidates(
        hypotheses=["H"],
        evidence=["F"],
        involves=_involves(("H", "a"), ("H", "b"), ("F", "a"), ("F", "b")),
    )
    assert len(cands) == 1
    assert cands[0].shared_entities == frozenset({"a", "b"})


def test_direction_is_evidence_to_hypothesis_only() -> None:
    """Two hypotheses sharing an entity are not paired with each other; only evidence→hypothesis,
    and an entity a node shares with *itself* never makes a self-candidate."""
    cands = structural_entity_candidates(
        hypotheses=["H1", "H2"],
        evidence=["F"],
        involves=_involves(("H1", "a"), ("H2", "a"), ("F", "a")),
    )
    pairs = {c.key for c in cands}
    assert pairs == {("F", "H1"), ("F", "H2")}
    assert ("H1", "H2") not in pairs and ("H2", "H1") not in pairs


def test_entity_only_on_inactive_node_is_ignored() -> None:
    """An ``INVOLVES`` row whose node is in neither the hypotheses nor the evidence set (e.g. an
    inactive node the caller excluded) contributes no candidate."""
    cands = structural_entity_candidates(
        hypotheses=["H"],
        evidence=["F"],
        involves=_involves(("H", "a"), ("OTHER", "a")),  # F never shares -> no pair
    )
    assert cands == []


# --- the funnel: dedup, provenance merge, determinism ------------------------------------------


def test_funnel_merges_sources_and_entities_of_the_same_pair() -> None:
    g1 = [Candidate("F", "H", frozenset({CandidateSource.STRUCTURAL_ENTITY}), frozenset({"a"}))]
    g2 = [Candidate("F", "H", frozenset({CandidateSource.STRUCTURAL_ENTITY}), frozenset({"b"}))]
    pool = funnel(g1, g2)
    assert len(pool) == 1
    (c,) = pool.candidates
    assert c.shared_entities == frozenset({"a", "b"})


def test_funnel_is_deterministic_regardless_of_input_order() -> None:
    a = _structural("F2", "H")
    b = _structural("F1", "H")
    forward = funnel([a, b])
    reverse = funnel([b, a])
    assert [c.key for c in forward.candidates] == [c.key for c in reverse.candidates]
    assert [c.key for c in forward.candidates] == [("F1", "H"), ("F2", "H")]


def test_funnel_empty_is_empty_pool() -> None:
    assert funnel() == CandidatePool()
    assert len(funnel([])) == 0


def test_candidate_key_includes_direction() -> None:
    assert _structural("F", "H").key == ("F", "H")


def test_adapter_is_db_free_to_construct() -> None:
    # Mirrors the QBAF/derivation adapters: constructing the adapter touches no DB.
    assert isinstance(CandidateGenerationAdapter(), CandidateGenerationAdapter)
