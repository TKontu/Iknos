"""V9 / V14 — the pgvector `<=>` k-NN push-down matches the exact in-memory oracle.

Proves the two sides of the §5.1 candidate-generation seam are interchangeable: over ≥200
synthetic normalized vectors, the DB push-down (the adapter's `_embedding_knn_pushdown`) returns
the **same** candidates as the exact in-memory oracle (`embedding_knn_candidates`) at equal k, and
a **subset** when a node has several propositions. V14 (verified by EXPLAIN here) corrects the V9
premise: the push-down's `proposition_id IN (...)` filter makes the planner fetch the bounded set
through the proposition_id index and sort it **exactly** — it does *not* use the HNSW index (which
is the unbounded-k-NN mechanism), so there is no recall gap and no post-filter starvation for this
query. Also covers the G1.16 cross-model guard and the deliberate-tie boundary. Requires a live
pgvector DB (the `tests` CI workflow); the autouse `_isolate_db` fixture truncates the tables.
"""

from __future__ import annotations

import math
import random
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.candidates import (
    CandidateGenerationAdapter,
    CandidateSource,
    EmbeddedNode,
    embedding_knn_candidates,
    knn_pushdown_stmt,
)
from iknos.db.orm import DocumentContent, PropositionEmbedding

pytestmark = pytest.mark.asyncio

MODEL = "test-knn-model"
OTHER_MODEL = "test-knn-other-model"  # a second vector space — the G1.16 guard must exclude it
DIM = 1024
N_EVIDENCE = 220  # ≥ 200 synthetic vectors (V9 spec)
N_HYP = 4
K = 10


def _unit_vector(rng: random.Random) -> list[float]:
    v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def _seed(
    session: AsyncSession,
) -> tuple[list[EmbeddedNode], list[EmbeddedNode], dict[str, list[str]], set[str]]:
    """Insert one document + N_EVIDENCE evidence vectors; return the embedded nodes + mappings."""
    rng = random.Random(20260612)
    doc_id = uuid.uuid4()
    session.add(DocumentContent(document_id=doc_id, raw_text="synthetic"))

    ev_embedded: list[EmbeddedNode] = []
    prop_to_ev_nodes: dict[str, list[str]] = {}
    evidence_prop_ids: set[str] = set()
    for _ in range(N_EVIDENCE):
        prop_id = uuid.uuid4()
        vec = _unit_vector(rng)
        session.add(
            PropositionEmbedding(
                proposition_id=prop_id, document_id=doc_id, embedding=vec, model=MODEL
            )
        )
        # One proposition per evidence node, so proposition-rank == node-rank (clean comparison).
        node_id = str(prop_id)
        ev_embedded.append(EmbeddedNode(node=node_id, model=MODEL, vector=tuple(vec)))
        prop_to_ev_nodes[str(prop_id)] = [node_id]
        evidence_prop_ids.add(str(prop_id))
    await session.commit()

    hyp_embedded = [
        EmbeddedNode(node=f"hyp-{i}", model=MODEL, vector=tuple(_unit_vector(rng)))
        for i in range(N_HYP)
    ]
    return hyp_embedded, ev_embedded, prop_to_ev_nodes, evidence_prop_ids


async def test_pushdown_equals_the_exact_oracle(session: AsyncSession) -> None:
    hyp_embedded, ev_embedded, prop_to_ev_nodes, evidence_prop_ids = await _seed(session)

    # The push-down is an exact sort over the bounded active set (the `IN` filter bypasses the HNSW
    # index), so with one proposition per node and no float ties it reproduces the oracle exactly —
    # no ef_search tuning needed. Subset is the always-true invariant; equality is the strong claim.
    pushdown = await CandidateGenerationAdapter()._embedding_knn_pushdown(
        session,
        hyp_embedded=hyp_embedded,
        evidence_prop_ids=evidence_prop_ids,
        prop_to_evidence_nodes=prop_to_ev_nodes,
        k=K,
    )
    exact = embedding_knn_candidates(hypotheses=hyp_embedded, evidence=ev_embedded, k=K)

    pushdown_keys = {c.key for c in pushdown}
    exact_keys = {c.key for c in exact}
    assert pushdown_keys, "push-down returned no candidates"
    assert pushdown_keys <= exact_keys, "push-down proposed a pair the exact ranking would not at k"
    assert pushdown_keys == exact_keys, (
        "the bounded exact sort must reproduce the in-memory oracle; "
        f"missing {exact_keys - pushdown_keys}, extra {pushdown_keys - exact_keys}"
    )
    # All push-down candidates carry the same source tag as the exact path (interchangeable).
    assert all(c.sources == frozenset({CandidateSource.EMBEDDING_KNN}) for c in pushdown)
    # Each hypothesis gets at most k candidates (the contract bound).
    for h in {c.hypothesis for c in pushdown}:
        assert sum(1 for c in pushdown if c.hypothesis == h) <= K


async def test_pushdown_never_compares_across_embedding_models(session: AsyncSession) -> None:
    # G1.16: cosine across two embedding spaces is meaningless. The evidence is embedded under
    # MODEL but the hypothesis query vector declares OTHER_MODEL, so the `WHERE model =` guard must
    # exclude every evidence row — the push-down returns nothing, never a cross-space neighbour.
    _hyp, _ev, prop_to_ev_nodes, evidence_prop_ids = await _seed(session)
    rng = random.Random(7)
    cross_model_hyp = [
        EmbeddedNode(node="hyp-x", model=OTHER_MODEL, vector=tuple(_unit_vector(rng)))
    ]

    await session.execute(text("SET LOCAL hnsw.ef_search = 400"))
    pushdown = await CandidateGenerationAdapter()._embedding_knn_pushdown(
        session,
        hyp_embedded=cross_model_hyp,
        evidence_prop_ids=evidence_prop_ids,
        prop_to_evidence_nodes=prop_to_ev_nodes,
        k=K,
    )
    assert pushdown == [], "G1.16 guard breached: a candidate was proposed across embedding models"


async def test_pushdown_query_is_index_driven_not_a_seq_scan(session: AsyncSession) -> None:
    # V14 (EXPLAIN the real query): the production statement restricts to the active set with
    # `proposition_id IN (...)`. Against that selective predicate the planner fetches the bounded
    # set through the `ix_proposition_embeddings_proposition_id` index and sorts it exactly by
    # `<=>` — it does NOT use the HNSW index (verified: same plan across default / seqscan-off /
    # iterative_scan). The HNSW index is the *unbounded* k-NN mechanism; a query already bounded to
    # a candidate set neither needs nor benefits from it. So assert the real query is index-driven
    # via the proposition_id index (not a seq scan), and that it does not pull in the HNSW index.
    _hyp, _ev, _map, evidence_prop_ids = await _seed(session)
    evidence_uuids = [uuid.UUID(p) for p in evidence_prop_ids]
    stmt = knn_pushdown_stmt(
        model=MODEL, query_vector=[0.03] * DIM, evidence_uuids=evidence_uuids, k=K
    )
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})

    await session.execute(text("SET LOCAL enable_seqscan = off"))  # tiny table — force the index
    plan = await session.execute(text(f"EXPLAIN {compiled}"))
    plan_text = "\n".join(r[0] for r in plan)
    assert "ix_proposition_embeddings_proposition_id" in plan_text, plan_text
    assert "ix_proposition_embeddings_embedding_hnsw" not in plan_text, plan_text


def _axis_vector(axis: int, *, tilt: float = 0.0, tilt_axis: int = 2) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    v[tilt_axis] = tilt
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def test_pushdown_is_exact_over_the_bounded_active_set_no_starvation(
    session: AsyncSession,
) -> None:
    # V14: because the `IN` filter makes the query an exact sort over the active set (not an HNSW
    # scan — see test_pushdown_query_is_index_driven_not_a_seq_scan), there is no post-filter
    # "starvation": every active evidence row is considered even at the default ef_search and even
    # when the active set is a tiny fraction of the table. Seed many decoy rows NEAR the hypothesis
    # (not active) and a few active rows FAR from it; the push-down must still find all the active
    # rows the oracle finds — no tuning needed.
    doc_id = uuid.uuid4()
    session.add(DocumentContent(document_id=doc_id, raw_text="bounded"))
    hyp_vec = _axis_vector(0)  # hypothesis points along axis 0
    for i in range(200):  # decoys near the hypothesis (axis 0), NOT in the active set
        session.add(
            PropositionEmbedding(
                proposition_id=uuid.uuid4(),
                document_id=doc_id,
                embedding=_axis_vector(0, tilt=0.001 * i),
                model=MODEL,
            )
        )
    active_ev: list[EmbeddedNode] = []
    prop_to_ev: dict[str, list[str]] = {}
    active_ids: set[str] = set()
    for i in range(3):  # active evidence FAR from the hypothesis (axis 1) — real but distant
        pid = uuid.uuid4()
        vec = _axis_vector(1, tilt=0.001 * i)
        session.add(
            PropositionEmbedding(proposition_id=pid, document_id=doc_id, embedding=vec, model=MODEL)
        )
        node = str(pid)
        active_ev.append(EmbeddedNode(node=node, model=MODEL, vector=tuple(vec)))
        prop_to_ev[str(pid)] = [node]
        active_ids.add(str(pid))
    await session.commit()

    hyp = [EmbeddedNode(node="hyp", model=MODEL, vector=tuple(hyp_vec))]
    exact = embedding_knn_candidates(hypotheses=hyp, evidence=active_ev, k=K)
    assert len({c.key for c in exact}) == 3, "the oracle sees all three active evidence rows"

    # Default ef_search, no tuning: the bounded exact sort finds every active row (no starvation).
    pushdown = await CandidateGenerationAdapter()._embedding_knn_pushdown(
        session,
        hyp_embedded=hyp,
        evidence_prop_ids=active_ids,
        prop_to_evidence_nodes=prop_to_ev,
        k=K,
    )
    assert {c.key for c in pushdown} == {c.key for c in exact}, (
        "the bounded push-down must find every active evidence row — no HNSW post-filter starvation"
    )


async def test_pushdown_tie_break_may_diverge_from_the_oracle_at_the_limit_boundary(
    session: AsyncSession,
) -> None:
    # V14 point 3: two evidence propositions with IDENTICAL vectors (a genuine distance tie) on two
    # different nodes; at k=1 only one survives the LIMIT. The oracle breaks the tie by node id; the
    # push-down's LIMIT is on proposition order (node ids can't enter the SQL without defeating the
    # index), so it may keep the other node — the documented subset-claim caveat, pinned here.
    doc_id = uuid.uuid4()
    session.add(DocumentContent(document_id=doc_id, raw_text="tie"))
    vec = _unit_vector(random.Random(1))
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    for pid in (p1, p2):
        session.add(
            PropositionEmbedding(proposition_id=pid, document_id=doc_id, embedding=vec, model=MODEL)
        )
    await session.commit()
    n1, n2 = str(p1), str(p2)  # one proposition per node ⇒ node id == proposition id
    hyp = [EmbeddedNode(node="hyp", model=MODEL, vector=tuple(vec))]
    ev = [
        EmbeddedNode(node=n1, model=MODEL, vector=tuple(vec)),
        EmbeddedNode(node=n2, model=MODEL, vector=tuple(vec)),
    ]

    await session.execute(text("SET LOCAL hnsw.ef_search = 400"))
    pushdown = await CandidateGenerationAdapter()._embedding_knn_pushdown(
        session,
        hyp_embedded=hyp,
        evidence_prop_ids={n1, n2},
        prop_to_evidence_nodes={n1: [n1], n2: [n2]},
        k=1,
    )
    exact = embedding_knn_candidates(hypotheses=hyp, evidence=ev, k=1)

    assert len(pushdown) == 1 and len(exact) == 1  # k=1 → each keeps exactly one tied node
    assert exact[0].evidence == min(n1, n2)  # the oracle's deterministic node-id tie-break
    assert pushdown[0].evidence in {n1, n2}  # the push-down may keep either — the documented gap
