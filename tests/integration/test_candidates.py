"""G4.2 integration test — candidate generation reads real AGE + pgvector (architecture §5.1).

Real Postgres+AGE+pgvector. Two stages exercised end-to-end against the live store:

- **Stage 1 (structural-entity prior).** Seeds an active box with a ``Hypothesis`` and two
  ``Fact``s that share an ``Actor`` (via ``INVOLVES``) plus an unrelated ``Fact``, and asserts
  :meth:`CandidateGenerationAdapter.generate` proposes the entity-sharing pairs, excludes the
  unrelated fact, and excludes a deprecated-box fact. The headline case is the **dissimilar
  refuter**: a fact that shares the hypothesis's entity is proposed even though nothing about it
  is similar — the §5.1 recall guarantee.
- **Stage 2 (embedding k-NN).** Seeds a ``Hypothesis`` and ``Fact``s each ``EVIDENCED_BY`` a
  ``Proposition`` with a row in ``proposition_embeddings``, and asserts the cross-store chain
  (node → proposition → pgvector) yields ``EMBEDDING_KNN`` candidates for the near claims, while a
  different-``model`` embedding (the G1.16 identity guard) and a deprecated-box claim are excluded.

``generate`` reads the whole active subgraph, so on the shared CI/dev DB these are **containment**
assertions (my seeded pairs are present / my excluded nodes are absent), never global equality.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.candidates import CandidateGenerationAdapter, CandidateSource
from iknos.db.age import bootstrap_session, merge_edge, merge_vertex
from iknos.db.orm import PropositionEmbedding
from iknos.types.nodes import Box, BoxStatus

pytestmark = pytest.mark.asyncio

_KNN_MODEL = "g42-knn-model"  # the vector-space identity shared by the in-space embeddings


def _vec(*nonzero: tuple[int, float]) -> list[float]:
    """A 1024-d vector with the given ``(index, value)`` components set, rest zero."""
    v = [0.0] * 1024
    for i, x in nonzero:
        v[i] = x
    return v


async def _put_proposition(session: AsyncSession) -> uuid.UUID:
    pid = uuid.uuid4()
    await merge_vertex(session, "Proposition", {"id": str(pid), "text": "claim"})
    return pid


async def _put_evidenced_by(session: AsyncSession, *, node: uuid.UUID, prop: uuid.UUID) -> None:
    await merge_edge(session, src_id=node, dst_id=prop, label="EVIDENCED_BY", props={})


async def _embed_node(
    session: AsyncSession,
    *,
    node: uuid.UUID,
    document_id: uuid.UUID,
    vector: list[float],
    model: str = _KNN_MODEL,
) -> None:
    """Give a reasoning node a claim with a dense vector: node -EVIDENCED_BY-> Proposition row."""
    prop = await _put_proposition(session)
    await _put_evidenced_by(session, node=node, prop=prop)
    session.add(
        PropositionEmbedding(
            proposition_id=prop, document_id=document_id, embedding=vector, model=model
        )
    )


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_node(session: AsyncSession, label: str, box: uuid.UUID) -> uuid.UUID:
    nid = uuid.uuid4()
    await merge_vertex(
        session, label, {"id": str(nid), "box": str(box), "confidence": 1.0, "valid_to": None}
    )
    return nid


async def _put_entity(session: AsyncSession, box: uuid.UUID) -> uuid.UUID:
    eid = uuid.uuid4()
    await merge_vertex(session, "Actor", {"id": str(eid), "box": str(box), "valid_to": None})
    return eid


async def _put_involves(
    session: AsyncSession, *, node: uuid.UUID, entity: uuid.UUID, role: str
) -> None:
    await merge_edge(session, src_id=node, dst_id=entity, label="INVOLVES", props={"role": role})


async def test_generate_proposes_entity_sharing_pairs_and_excludes_the_rest(
    session: AsyncSession,
) -> None:
    await bootstrap_session(session)
    box = case_box("g42-candidates", "1", "test", 0.8)
    await _put_box(session, box)

    actor = await _put_entity(session, box.id)
    h = await _put_node(session, "Hypothesis", box.id)
    # f_support and f_refute both share the actor with the hypothesis -> both candidates.
    f_support = await _put_node(session, "Fact", box.id)
    f_refute = await _put_node(session, "Fact", box.id)
    # f_other shares no entity -> not a candidate.
    f_other = await _put_node(session, "Fact", box.id)

    await _put_involves(session, node=h, entity=actor, role="subject")
    await _put_involves(session, node=f_support, entity=actor, role="subject")
    await _put_involves(session, node=f_refute, entity=actor, role="object")
    await session.commit()

    pool = await CandidateGenerationAdapter().generate(session)
    pairs = {c.key for c in pool.candidates}
    hs, fs, fr, fo = str(h), str(f_support), str(f_refute), str(f_other)

    # Both entity-sharing facts are proposed (the dissimilar refuter f_refute among them — the
    # §5.1 recall guarantee), the unrelated fact is not, and no hypothesis→evidence inversion.
    assert {(fs, hs), (fr, hs)} <= pairs
    assert (fo, hs) not in pairs
    assert (hs, fs) not in pairs

    cand = next(c for c in pool.candidates if c.key == (fs, hs))
    assert CandidateSource.STRUCTURAL_ENTITY in cand.sources
    assert str(actor) in cand.shared_entities


async def test_generate_excludes_deprecated_box_evidence(session: AsyncSession) -> None:
    await bootstrap_session(session)
    box = case_box("g42-active", "1", "test", 0.8)
    await _put_box(session, box)
    dead = case_box("g42-dead", "1", "test", 0.5).model_copy(
        update={"status": BoxStatus.DEPRECATED}
    )
    await _put_box(session, dead)

    actor = await _put_entity(session, box.id)
    h = await _put_node(session, "Hypothesis", box.id)
    # A fact in the deprecated box, sharing the actor — must NOT be proposed (active-box scope).
    dead_fact = await _put_node(session, "Fact", dead.id)
    await _put_involves(session, node=h, entity=actor, role="subject")
    await _put_involves(session, node=dead_fact, entity=actor, role="subject")
    await session.commit()

    pool = await CandidateGenerationAdapter().generate(session)
    assert (str(dead_fact), str(h)) not in {c.key for c in pool.candidates}


async def test_generate_proposes_embedding_knn_and_respects_the_model_guard_and_scope(
    session: AsyncSession,
) -> None:
    """Stage 2 end-to-end: the cross-store node → proposition → pgvector chain yields EMBEDDING_KNN
    candidates for the near claims; a different-model embedding (G1.16 guard) and a deprecated-box
    claim are excluded."""
    await bootstrap_session(session)
    box = case_box("g42-knn-active", "1", "test", 0.8)
    await _put_box(session, box)
    dead = case_box("g42-knn-dead", "1", "test", 0.5).model_copy(
        update={"status": BoxStatus.DEPRECATED}
    )
    await _put_box(session, dead)

    doc_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO document_content (document_id, raw_text) VALUES (:id, :t)"),
        {"id": doc_id, "t": "g42 knn corpus"},
    )

    # The hypothesis points in a distinctive direction; near/mid are close, the guarded ones aren't.
    h = await _put_node(session, "Hypothesis", box.id)
    await _embed_node(session, node=h, document_id=doc_id, vector=_vec((0, 1.0)))

    f_near = await _put_node(session, "Fact", box.id)
    await _embed_node(session, node=f_near, document_id=doc_id, vector=_vec((0, 1.0), (1, 0.1)))
    f_mid = await _put_node(session, "Fact", box.id)
    await _embed_node(session, node=f_mid, document_id=doc_id, vector=_vec((0, 1.0), (1, 1.0)))

    # Same near vector but a DIFFERENT embedding model -> cosine is meaningless -> never paired.
    f_other_model = await _put_node(session, "Fact", box.id)
    await _embed_node(
        session, node=f_other_model, document_id=doc_id, vector=_vec((0, 1.0)), model="g42-other"
    )

    # A near claim in a deprecated box -> outside the active scope -> excluded before the k-NN.
    f_dead = await _put_node(session, "Fact", dead.id)
    await _embed_node(session, node=f_dead, document_id=doc_id, vector=_vec((0, 1.0)))
    await session.commit()

    # Generous k so the seeded near/mid are within top-k regardless of other rows on the shared DB;
    # the model/scope exclusions hold at any k (they are filtered before the k-NN runs).
    pool = await CandidateGenerationAdapter().generate(session, k=1000)
    by_key = {c.key: c for c in pool.candidates}
    hs = str(h)

    # The cross-store chain works: both near claims are proposed as embedding candidates.
    assert (str(f_near), hs) in by_key
    assert (str(f_mid), hs) in by_key
    assert CandidateSource.EMBEDDING_KNN in by_key[(str(f_near), hs)].sources

    # The G1.16 vector-space guard and the active-box scope both exclude their claim.
    assert (str(f_other_model), hs) not in by_key
    assert (str(f_dead), hs) not in by_key
