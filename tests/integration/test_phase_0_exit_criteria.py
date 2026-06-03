"""Phase 0 exit-criteria smoke test (docs/todo_phase_0_foundations.md).

Demonstrates:
1. A document and a span are stored; span text resolves via local join.
2. A reasoning node (Fact) and an evidential edge (SUPPORTS) are created
   carrying box, tier, both annotations, and bitemporal fields.
3. An Action record is written and linked to the produced node.
4. The schema in code matches §10 (passing the above is the demonstration).
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.age import bootstrap_session, execute_cypher
from iknos.db.spans import resolve_span_text
from iknos.provenance.action_log import record_action

pytestmark = pytest.mark.asyncio


def _props(d: dict) -> str:
    """Inline a dict into a Cypher map literal.

    AGE's cypher() does not bind parameters into the Cypher body, so values
    must be serialized into the query text. For tests we control the inputs;
    in production code a proper Cypher builder will replace this.
    """
    parts: list[str] = []
    for k, v in d.items():
        if isinstance(v, str):
            esc = v.replace("\\", "\\\\").replace("'", "\\'")
            parts.append(f"{k}: '{esc}'")
        elif isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
        elif v is None:
            parts.append(f"{k}: null")
        else:
            esc = json.dumps(v).replace("'", "\\'")
            parts.append(f"{k}: '{esc}'")
    return "{" + ", ".join(parts) + "}"


async def test_phase_0_end_to_end(session: AsyncSession) -> None:
    await bootstrap_session(session)

    # --- 1. Document + Span, with text resolved via local join ---
    doc_id = uuid.uuid4()
    raw = "The 2019 flood-defense budget was insufficient. The next year saw record damage."
    await session.execute(
        text(
            "INSERT INTO document_content (document_id, raw_text, title) "
            "VALUES (:id, :text, :title)"
        ),
        {"id": doc_id, "text": raw, "title": "Smith 2021 report"},
    )

    await execute_cypher(
        session,
        f"CREATE (:Document {_props({'id': str(doc_id), 'title': 'Smith 2021 report'})})",
    )

    span_id = uuid.uuid4()
    span_start, span_end = 0, 49
    span_props = _props(
        {"id": str(span_id), "document_id": str(doc_id), "start": span_start, "end": span_end}
    )
    await execute_cypher(
        session,
        f"CREATE (:Span {span_props})",
    )

    resolved = await resolve_span_text(session, doc_id, span_start, span_end)
    assert resolved == raw[span_start:span_end]

    # --- 2. Reasoning node + evidential edge, both carrying annotations + bitemporal ---
    box_id = uuid.uuid4()
    now = datetime.now(UTC)
    await execute_cypher(
        session,
        "CREATE (:Box "
        + _props(
            {
                "id": str(box_id),
                "name": "default-domain",
                "tier": "domain",
                "version": "0.1.0",
                "source": "test",
                "reliability_prior": 0.9,
                "valid_from": now.isoformat(),
                "status": "active",
            }
        )
        + ")",
    )

    fact_a_id, fact_b_id = uuid.uuid4(), uuid.uuid4()
    for fid, statement in (
        (fact_a_id, "The 2019 flood-defense budget was insufficient."),
        (fact_b_id, "Flood defenses were inadequate in 2019."),
    ):
        await execute_cypher(
            session,
            "CREATE (:Fact "
            + _props(
                {
                    "id": str(fid),
                    "box": str(box_id),
                    "tier": "evidence" if fid == fact_a_id else "derived",
                    "statement": statement,
                    "support_count": 1,
                    "confidence": 0.8,
                    "event_time": "2019-01-01T00:00:00+00:00",
                    "ingested_at": now.isoformat(),
                    "valid_from": now.isoformat(),
                }
            )
            + ")",
        )

    await execute_cypher(
        session,
        f"MATCH (a:Fact {{id: '{fact_a_id}'}}), (b:Fact {{id: '{fact_b_id}'}}) "
        f"CREATE (a)-[:SUPPORTS "
        + _props(
            {
                "box": str(box_id),
                "sign": "supports",
                "strength": 0.7,
                "significance": 0.6,
                "support_count": 1,
                "confidence": 0.7,
                "ingested_at": now.isoformat(),
                "valid_from": now.isoformat(),
            }
        )
        + "]->(b)",
    )

    await execute_cypher(
        session,
        f"MATCH (f:Fact {{id: '{fact_a_id}'}}), (s:Span {{id: '{span_id}'}}) "
        "CREATE (f)-[:EVIDENCED_BY]->(s)",
    )

    rows = await execute_cypher(
        session,
        f"MATCH (a:Fact {{id: '{fact_a_id}'}})-[r:SUPPORTS]->(:Fact) RETURN r",
        returns="r agtype",
    )
    assert len(rows) == 1

    # --- 3. Action log, linked to the created node ---
    action_id = await record_action(
        session,
        actor="test:phase_0_smoke",
        action_type="create_fact",
        inputs={"span_id": str(span_id), "box_id": str(box_id)},
        outputs={"fact_id": str(fact_a_id)},
    )
    await session.commit()

    row = await session.execute(
        text("SELECT actor, action_type, outputs FROM actions WHERE id = :id"),
        {"id": action_id},
    )
    rec = row.one()
    assert rec.actor == "test:phase_0_smoke"
    assert rec.outputs["fact_id"] == str(fact_a_id)
