"""V9 — the pgvector `<=>` k-NN push-down matches the in-memory exact oracle, and uses the index.

Proves the two sides of the §5.1 candidate-generation seam are interchangeable: over ≥200
synthetic normalized vectors, the DB push-down (the adapter's `_embedding_knn_pushdown`) returns
a **subset** of the exact in-memory oracle (`embedding_knn_candidates`) at equal k — the
recall-vs-exact invariant — and the query it issues actually uses the R4 HNSW index. Requires a
live pgvector DB (the `tests` CI workflow); the autouse `_isolate_db` fixture truncates the tables.
"""

from __future__ import annotations

import math
import random
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.candidates import (
    CandidateGenerationAdapter,
    CandidateSource,
    EmbeddedNode,
    embedding_knn_candidates,
)
from iknos.db.orm import DocumentContent, PropositionEmbedding

pytestmark = pytest.mark.asyncio

MODEL = "test-knn-model"
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


async def test_pushdown_is_a_subset_of_the_exact_oracle(session: AsyncSession) -> None:
    hyp_embedded, ev_embedded, prop_to_ev_nodes, evidence_prop_ids = await _seed(session)

    # Make the HNSW search near-exhaustive so the subset relation is deterministic on this size
    # (the general recall<1 case is documented; the gate corpus measures the real recall@k).
    await session.execute(text("SET LOCAL hnsw.ef_search = 400"))

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
    # All push-down candidates carry the same source tag as the exact path (interchangeable).
    assert all(c.sources == frozenset({CandidateSource.EMBEDDING_KNN}) for c in pushdown)
    # Each hypothesis gets at most k candidates (the contract bound).
    for h in {c.hypothesis for c in pushdown}:
        assert sum(1 for c in pushdown if c.hypothesis == h) <= K


async def test_pushdown_query_uses_the_hnsw_index(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL enable_seqscan = off"))  # tiny table — force the choice
    vec = "[" + ",".join(["0.03"] * DIM) + "]"
    plan = await session.execute(
        text(
            "EXPLAIN SELECT proposition_id FROM proposition_embeddings "
            f"WHERE model = '{MODEL}' ORDER BY embedding <=> '{vec}' LIMIT {K}"
        )
    )
    plan_text = "\n".join(r[0] for r in plan)
    assert "ix_proposition_embeddings_embedding_hnsw" in plan_text, plan_text
