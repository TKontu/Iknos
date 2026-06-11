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
    EmbeddedNode,
    FunnelStrategy,
    InvolvesRow,
    embedding_knn_candidates,
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


# --- stage 2: the embedding k-NN prior (§5.1) -------------------------------------------------


def _embedded(node: str, *vector: float, model: str = "m") -> EmbeddedNode:
    """An ``(node, model, vector)`` triple — one of the node's EVIDENCED_BY proposition vectors."""
    return EmbeddedNode(node=node, model=model, vector=tuple(vector))


# --- the decision fixture (§5.1): recall-first top-k over a similarity floor -------------------


def test_decision_fixture_knn_topk_keeps_dissimilar_refuter_a_floor_drops_it() -> None:
    """The embedding analogue of the union/intersect fixture, same dissimilar-refuter throughline:
    pure top-k keeps a real-but-dissimilar refuter; a cosine floor drops exactly that refuter."""
    h = _embedded("H", 1.0, 0.0)
    f_sup = _embedded("F_sup", 0.99, 0.14)  # near-parallel to H: high cosine (a supporter)
    f_ref = _embedded("F_ref", 0.20, 0.98)  # near-orthogonal: low cosine, but a genuine refuter

    recall = embedding_knn_candidates(hypotheses=[h], evidence=[f_sup, f_ref], k=10)
    assert {c.key for c in recall} == {("F_sup", "H"), ("F_ref", "H")}

    # A floor (the precision pre-filter, retained at the seam) re-introduces the support-bias.
    precision = embedding_knn_candidates(
        hypotheses=[h], evidence=[f_sup, f_ref], k=10, min_similarity=0.5
    )
    assert {c.key for c in precision} == {("F_sup", "H")}


def test_knn_limits_to_the_k_nearest() -> None:
    h = _embedded("H", 1.0, 0.0)
    near = _embedded("E_near", 1.0, 0.1)  # cosine ~0.995
    mid = _embedded("E_mid", 1.0, 1.0)  # cosine ~0.707
    far = _embedded("E_far", 0.0, 1.0)  # cosine 0.0
    pool = embedding_knn_candidates(hypotheses=[h], evidence=[near, mid, far], k=2)
    assert {c.key for c in pool} == {("E_near", "H"), ("E_mid", "H")}


def test_knn_only_compares_within_one_embedding_model() -> None:
    """The G1.16 vector-space identity guard: cosine across models is meaningless, so not paired."""
    h = _embedded("H", 1.0, 0.0, model="m1")
    same = _embedded("E_same", 1.0, 0.0, model="m1")
    other = _embedded("E_other", 1.0, 0.0, model="m2")  # identical vector, different space
    pool = embedding_knn_candidates(hypotheses=[h], evidence=[same, other], k=10)
    assert {c.key for c in pool} == {("E_same", "H")}


def test_knn_candidate_carries_embedding_source_and_is_unscored() -> None:
    h = _embedded("H", 1.0, 0.0)
    e = _embedded("E", 1.0, 0.0)
    [cand] = embedding_knn_candidates(hypotheses=[h], evidence=[e], k=1)
    assert cand.sources == frozenset({CandidateSource.EMBEDDING_KNN})
    assert cand.shared_entities == frozenset()  # the embedding stage supplies no entity rationale


def test_knn_node_with_several_propositions_is_one_candidate_by_its_best_match() -> None:
    h = _embedded("H", 1.0, 0.0)
    # Evidence node E is EVIDENCED_BY two propositions: one orthogonal, one parallel to H.
    e_far = EmbeddedNode(node="E", model="m", vector=(0.0, 1.0))
    e_near = EmbeddedNode(node="E", model="m", vector=(1.0, 0.0))
    pool = embedding_knn_candidates(hypotheses=[h], evidence=[e_far, e_near], k=10)
    assert [c.key for c in pool] == [("E", "H")]  # one candidate, not one per proposition

    # The best-matching prop represents the node: E clears a floor only its near prop passes.
    floored = embedding_knn_candidates(
        hypotheses=[h], evidence=[e_far, e_near], k=10, min_similarity=0.9
    )
    assert [c.key for c in floored] == [("E", "H")]


def test_knn_never_pairs_a_node_with_itself() -> None:
    # A node id appearing on both sides (a node that is also a target) is never self-paired.
    h = _embedded("X", 1.0, 0.0)
    e_self = _embedded("X", 1.0, 0.0)
    e_other = _embedded("E", 1.0, 0.0)
    pool = embedding_knn_candidates(hypotheses=[h], evidence=[e_self, e_other], k=10)
    assert {c.key for c in pool} == {("E", "X")}


def test_knn_is_deterministic_regardless_of_input_order() -> None:
    h = _embedded("H", 1.0, 0.0)
    a = _embedded("A", 1.0, 0.1)
    b = _embedded("B", 1.0, 0.1)  # cosine tie with A -> broken by node id
    fwd = embedding_knn_candidates(hypotheses=[h], evidence=[a, b], k=10)
    rev = embedding_knn_candidates(hypotheses=[h], evidence=[b, a], k=10)
    assert [c.key for c in fwd] == [c.key for c in rev] == [("A", "H"), ("B", "H")]


def test_knn_empty_inputs_and_nonpositive_k_yield_nothing() -> None:
    h = _embedded("H", 1.0, 0.0)
    e = _embedded("E", 1.0, 0.0)
    assert embedding_knn_candidates(hypotheses=[], evidence=[e], k=10) == []
    assert embedding_knn_candidates(hypotheses=[h], evidence=[], k=10) == []
    assert embedding_knn_candidates(hypotheses=[h], evidence=[e], k=0) == []


def test_funnel_unions_structural_and_embedding_sources() -> None:
    struct = _structural("F1", "H", "actorA")  # structural-only
    emb_same = Candidate(
        evidence="F1", hypothesis="H", sources=frozenset({CandidateSource.EMBEDDING_KNN})
    )
    emb_only = Candidate(
        evidence="F2", hypothesis="H", sources=frozenset({CandidateSource.EMBEDDING_KNN})
    )
    by_key = {c.key: c for c in funnel([struct], [emb_same, emb_only]).candidates}
    # The pair both stages found carries both sources + the structural entity rationale.
    assert by_key[("F1", "H")].sources == {
        CandidateSource.STRUCTURAL_ENTITY,
        CandidateSource.EMBEDDING_KNN,
    }
    assert by_key[("F1", "H")].shared_entities == frozenset({"actorA"})
    # The embedding-only pair survives under the recall-first UNION default.
    assert ("F2", "H") in by_key
