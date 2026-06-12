"""Unit tests for the pure action-record constructor (§10.1, V11).

``record_action`` is otherwise all-DB (it adds + flushes against an ``AsyncSession``); the
DB-touching round-trip is exercised by every Phase 1–4 integration test that writes an
operator Action. Here we pin the *pure* seam ``build_action``: the field mapping every
operator's provenance record flows through, the one piece of defaulting logic, and the
§10.1 invariant that the payloads are JSON-serializable — no database needed.
"""

import json
import uuid

from iknos.provenance.action_log import build_action

# id/timestamp are DB-side server defaults (gen_random_uuid()/NOW()), so a freshly built,
# un-flushed Action carries neither — the persisted values appear only after record_action
# flushes. These tests deliberately never assert on id/timestamp.


def test_maps_all_fields_onto_the_row() -> None:
    span = uuid.uuid4()
    fact = uuid.uuid4()
    action = build_action(
        actor="extractor",
        action_type="extract",
        inputs={"target_span": str(span)},
        outputs={"fact": str(fact)},
        model="claude-opus-4-8",
        sampling={"n_samples": 3, "temperature": 0.0},
        raw_judgment="raw model text",
        calibration={"curve": "identity"},
    )

    assert action.actor == "extractor"
    assert action.action_type == "extract"
    assert action.inputs == {"target_span": str(span)}
    assert action.outputs == {"fact": str(fact)}
    assert action.model == "claude-opus-4-8"
    assert action.sampling == {"n_samples": 3, "temperature": 0.0}
    assert action.raw_judgment == "raw model text"
    assert action.calibration == {"curve": "identity"}


def test_id_and_timestamp_are_unset_before_flush() -> None:
    # They are server-side defaults — the pure constructor must not invent them (that would
    # be a non-replayable client-side clock/uuid, the hazard the script-runtime guards against).
    action = build_action(actor="parser", action_type="parse")
    assert action.id is None
    assert action.timestamp is None


def test_missing_inputs_and_outputs_default_to_empty_dict() -> None:
    # The §10.1 provenance edges always read an object off inputs/outputs; None would break
    # the `inputs->>'...'` functional-index lookups that back audit reach-back.
    action = build_action(actor="parser", action_type="parse")
    assert action.inputs == {}
    assert action.outputs == {}


def test_explicit_payloads_pass_through_unchanged() -> None:
    inputs = {"document_id": str(uuid.uuid4())}
    outputs = {"spans": [str(uuid.uuid4()), str(uuid.uuid4())]}
    action = build_action(actor="segmenter", action_type="segment", inputs=inputs, outputs=outputs)
    assert action.inputs == inputs
    assert action.outputs == outputs


def test_optional_fields_stay_none_when_absent() -> None:
    # Absent → None, never zeroed/empty-stringed (so observability can tell "no model" from
    # "model recorded as ''"); mirrors the metrics-key discipline in the maintenance backlog.
    action = build_action(actor="resolver", action_type="resolve")
    assert action.model is None
    assert action.sampling is None
    assert action.raw_judgment is None
    assert action.calibration is None


def test_metrics_defaults_to_empty_dict_and_passes_through() -> None:
    # R12 observability floor: like inputs/outputs, metrics coerces None → {} so every Action row
    # has an object to read (the column is NOT NULL DEFAULT '{}'); an explicit payload passes
    # through unchanged.
    assert build_action(actor="parser", action_type="parse").metrics == {}
    metrics = {"duration_ms": 12, "n_samples": 3, "prompt_tokens": 40, "cache_hit": False}
    action = build_action(actor="extractor", action_type="extract", metrics=metrics)
    assert action.metrics == metrics


def test_payloads_are_json_serializable() -> None:
    # The four payload columns are JSONB; whatever an operator passes must round-trip through
    # JSON or the flush fails opaquely. Pin it here on representative provenance payloads.
    action = build_action(
        actor="edge_judge",
        action_type="judge_edge",
        inputs={"hypothesis": str(uuid.uuid4()), "evidence": [1, 2, 3]},
        outputs={"sign": "REFUTES", "sign_stable": True},
        sampling={"n_samples": 5, "seed": None},
        calibration={"raw": 0.7, "calibrated": 0.62},
    )
    for payload in (action.inputs, action.outputs, action.sampling, action.calibration):
        assert json.loads(json.dumps(payload)) == payload
