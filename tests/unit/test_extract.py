"""Unit tests for the ``extract`` operator core (Phase 2, G2.2).

DB-free: the pure helpers (schema defaults, prompt, annotation seed, the Fact write
contract) and the LLM inference path (mocked ``guided_complete``). The full persist +
idempotency path runs against live AGE in ``tests/integration/test_extract.py`` (CI).
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from iknos.core.extract import (
    ENTITY_SCHEMA,
    EXTRACT_SCHEMA_VERSION,
    Extractor,
    NodeKind,
    Role,
    _EntityOut,
    base_annotations,
    build_messages,
    fact_to_props,
    seed_confidence,
)
from iknos.types.annotations import Annotations
from iknos.types.governance import Sensitivity, SensitivityLevel
from iknos.types.nodes import Fact, Tier
from iknos.types.temporal import BitemporalFields

# --- structured-output contract (guided decoding) ---


def test_entity_out_defaults_for_bare_label():
    # A bare {"label": ...} response still validates via defaults (mirrors _PropositionOut).
    e = _EntityOut.model_validate({"label": "bearing"})
    assert e.label == "bearing"
    assert e.type == ""
    assert e.kind is NodeKind.OBJECT
    assert e.role is Role.OTHER


def test_entity_out_full_record():
    e = _EntityOut.model_validate(
        {"label": "operator", "type": "person", "kind": "actor", "role": "subject"}
    )
    assert e.kind is NodeKind.ACTOR
    assert e.role is Role.SUBJECT


def test_entity_schema_is_the_fact_entities_schema():
    # The guided-decode schema and the model the prompt vocab is generated from agree.
    assert "entities" in ENTITY_SCHEMA["properties"]


def test_build_messages_marks_statement():
    msgs = build_messages("The bearing failed.")
    assert msgs[0]["role"] == "system"
    assert "STATEMENT:\nThe bearing failed." in msgs[1]["content"]


# --- annotation seed (§12) ---


def test_seed_confidence_uses_faithfulness_when_present():
    assert seed_confidence(0.73) == 0.73


def test_seed_confidence_zero_faithfulness_is_not_swallowed():
    # 0.0 is a real (low) faithfulness — must NOT fall through to the 1.0 identity.
    assert seed_confidence(0.0) == 0.0


def test_seed_confidence_none_is_viterbi_identity():
    assert seed_confidence(None) == 1.0


def test_base_annotations_count_is_one_and_pair_uncollapsed():
    ann = base_annotations(0.6)
    assert ann.support_count == 1
    assert ann.confidence == 0.6
    ann_none = base_annotations(None)
    assert ann_none.support_count == 1
    assert ann_none.confidence == 1.0


# --- Fact write contract ---


def _fact(**over) -> Fact:
    now = datetime(2026, 6, 11, tzinfo=UTC)
    defaults = dict(
        id=uuid.uuid4(),
        box=uuid.uuid4(),
        tier=Tier.CASE,
        statement="The bearing failed.",
        annotations=Annotations(support_count=1, confidence=0.8),
        temporal=BitemporalFields(ingested_at=now, valid_from=now),
    )
    defaults.update(over)
    return Fact(**defaults)  # type: ignore[arg-type]


def test_fact_to_props_flattens_annotations_and_bitemporal():
    f = _fact()
    props = fact_to_props(f)
    assert props["id"] == str(f.id)
    assert props["box"] == str(f.box)
    assert props["tier"] == "case"
    assert props["statement"] == "The bearing failed."
    # Annotations flattened, never collapsed (§12).
    assert props["support_count"] == 1
    assert props["confidence"] == 0.8
    # Open bitemporal interval → valid_to null; event_time unset → null.
    assert props["valid_to"] is None
    assert props["event_time"] is None
    assert props["ingested_at"] == "2026-06-11T00:00:00+00:00"
    # Sensitivity via its canonical flat names (lattice origin by default, G2.6 seeds it).
    assert props["sensitivity_level"] == "public"
    assert props["sensitivity_compartments"] == []


def test_fact_to_props_serializes_event_time_and_sensitivity():
    et = datetime(2019, 1, 1, tzinfo=UTC)
    f = _fact(
        temporal=BitemporalFields(
            ingested_at=datetime(2026, 6, 11, tzinfo=UTC),
            valid_from=datetime(2026, 6, 11, tzinfo=UTC),
            event_time=et,
        ),
        sensitivity=Sensitivity(
            level=SensitivityLevel.CONFIDENTIAL, compartments=frozenset({"a", "b"})
        ),
    )
    props = fact_to_props(f)
    assert props["event_time"] == "2019-01-01T00:00:00+00:00"
    assert props["sensitivity_level"] == "confidential"
    assert props["sensitivity_compartments"] == ["a", "b"]


def test_fact_to_props_omits_override_for_machine_fact():
    # §10.3: override is null on machine-produced values — omitted, not written as null.
    assert "override" not in fact_to_props(_fact())


# --- inference path (mocked LLM, DB-free) ---


class _FakeLLM:
    def __init__(self, payload):
        self.model = "test-model"
        self._payload = payload
        self.calls: list = []

    async def guided_complete(self, messages, schema, sampling):
        self.calls.append((messages, schema, sampling))
        return self._payload


@pytest.mark.asyncio
async def test_infer_assigns_fresh_ids_and_maps_kind_role():
    llm = _FakeLLM(
        {
            "entities": [
                {"label": "operator", "type": "person", "kind": "actor", "role": "subject"},
                {"label": "pump", "type": "equipment", "kind": "object", "role": "object"},
            ]
        }
    )
    ex = Extractor(llm)  # type: ignore[arg-type]
    entities = await ex._infer(asyncio.Semaphore(2), "The operator restarted the pump.")

    assert [e.label for e in entities] == ["operator", "pump"]
    assert [e.kind for e in entities] == [NodeKind.ACTOR, NodeKind.OBJECT]
    assert [e.role for e in entities] == [Role.SUBJECT, Role.OBJECT]
    # Fresh, distinct ids — no dedup in this slice (G2.3).
    assert len({e.id for e in entities}) == 2
    # The guided-decode schema was the FactEntities schema.
    assert llm.calls[0][1] is ENTITY_SCHEMA


@pytest.mark.asyncio
async def test_infer_two_mentions_become_two_nodes_no_dedup():
    llm = _FakeLLM(
        {
            "entities": [
                {"label": "bearing", "kind": "object", "role": "subject"},
                {"label": "bearing", "kind": "object", "role": "object"},
            ]
        }
    )
    ex = Extractor(llm)  # type: ignore[arg-type]
    entities = await ex._infer(asyncio.Semaphore(1), "The bearing damaged the bearing housing.")
    assert len(entities) == 2
    assert entities[0].id != entities[1].id  # same label, still two fresh nodes


@pytest.mark.asyncio
async def test_infer_empty_returns_no_entities():
    ex = Extractor(_FakeLLM({"entities": []}))  # type: ignore[arg-type]
    assert await ex._infer(asyncio.Semaphore(1), "Well, anyway.") == []


def test_schema_version_is_recorded_constant():
    # Guards against silently changing the pipeline identity (cf. proposition layer).
    assert EXTRACT_SCHEMA_VERSION == 1
