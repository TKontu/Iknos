"""G4.2 integration test — candidate generation reads real AGE (architecture §5.1).

Real Postgres+AGE. Seeds an active box with a ``Hypothesis`` and two ``Fact``s that share an
``Actor`` (via ``INVOLVES``) plus an unrelated ``Fact``, and asserts
:meth:`CandidateGenerationAdapter.generate` proposes exactly the entity-sharing pairs (the
structural prior, stage 1), excludes the unrelated fact, and excludes a deprecated-box fact.
The headline case is the **dissimilar refuter**: a fact that shares the hypothesis's entity is
proposed as a candidate even though nothing about it is similar — the §5.1 recall guarantee.

``generate`` reads the whole active subgraph, so on the shared CI/dev DB these are **containment**
assertions (my seeded pairs are present / my excluded nodes are absent), never global equality.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.candidates import CandidateGenerationAdapter, CandidateSource
from iknos.db.age import bootstrap_session, merge_edge, merge_vertex
from iknos.types.nodes import Box, BoxStatus

pytestmark = pytest.mark.asyncio


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
