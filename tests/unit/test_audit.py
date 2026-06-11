"""Unit tests for the pure provenance-gap logic (Phase 2 G2.7).

The DB-touching reach-back (`fact_provenance`, `audit_box_facts`) is exercised by the
integration test against a live AGE graph; here we pin the *invariant* — what counts as
an auditability gap (§10.2) — on hand-built provenance objects, no database needed.
"""

import uuid

from iknos.provenance.audit import (
    MISSING_ACTION,
    MISSING_SOURCE_TEXT,
    MISSING_SPAN,
    FactProvenance,
    ProducingAction,
    SpanRef,
    provenance_gaps,
)


def _span(text: str | None) -> SpanRef:
    return SpanRef(
        span_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        start=0,
        end=10,
        text=text,
    )


def _action() -> ProducingAction:
    return ProducingAction(
        id=uuid.uuid4(),
        actor="extractor",
        action_type="extract",
        model="test-model",
    )


def _prov(*, spans: list[SpanRef], action: ProducingAction | None) -> FactProvenance:
    return FactProvenance(
        fact_id=uuid.uuid4(),
        proposition_id=uuid.uuid4(),
        spans=spans,
        action=action,
    )


def test_fully_provenanced_fact_has_no_gaps() -> None:
    prov = _prov(spans=[_span("the operator restarted the pump")], action=_action())
    assert provenance_gaps(prov) == frozenset()
    assert prov.is_auditable is True


def test_missing_action_is_a_gap() -> None:
    prov = _prov(spans=[_span("resolvable text")], action=None)
    assert provenance_gaps(prov) == frozenset({MISSING_ACTION})
    assert prov.is_auditable is False


def test_no_evidence_span_is_a_gap() -> None:
    prov = _prov(spans=[], action=_action())
    assert provenance_gaps(prov) == frozenset({MISSING_SPAN})
    assert prov.is_auditable is False


def test_span_without_resolvable_text_is_a_gap() -> None:
    # The Fact reaches a Span, but the Span's source text could not be resolved (e.g. the
    # document_content row is gone) — §10.2 requires reaching the source *text*, not just
    # the span node.
    prov = _prov(spans=[_span(None)], action=_action())
    assert provenance_gaps(prov) == frozenset({MISSING_SOURCE_TEXT})
    assert prov.is_auditable is False


def test_one_resolvable_span_among_several_satisfies_source_text() -> None:
    # Auditability needs *a* path to source text, not every span resolvable.
    prov = _prov(spans=[_span(None), _span("resolvable")], action=_action())
    assert provenance_gaps(prov) == frozenset()


def test_no_spans_reports_only_the_span_gap_not_the_text_gap() -> None:
    # MISSING_SPAN subsumes MISSING_SOURCE_TEXT — a missing span is not also reported as
    # missing text (that would be redundant noise in the violation set).
    prov = _prov(spans=[], action=None)
    assert provenance_gaps(prov) == frozenset({MISSING_SPAN, MISSING_ACTION})
