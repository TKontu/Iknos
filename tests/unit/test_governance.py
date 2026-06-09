"""Unit tests for governance value objects (§9.1) — pure algebra, no DB."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from iknos.types.annotations import Annotations
from iknos.types.edges import EdgeSign, EvidentialEdge
from iknos.types.governance import (
    _SENSITIVITY_RANK,
    Sensitivity,
    SensitivityLevel,
    SourceInterest,
)
from iknos.types.nodes import Box, Document, Fact, Span, Tier
from iknos.types.temporal import BitemporalFields

_NOW = datetime(2026, 6, 9, tzinfo=UTC)


def _annotations() -> Annotations:
    return Annotations(support_count=1, confidence=0.8)


def _temporal() -> BitemporalFields:
    return BitemporalFields(ingested_at=_NOW, valid_from=_NOW)


# --- lattice order ----------------------------------------------------------


def test_rank_is_public_to_restricted() -> None:
    ordered = [
        SensitivityLevel.PUBLIC,
        SensitivityLevel.INTERNAL,
        SensitivityLevel.CONFIDENTIAL,
        SensitivityLevel.RESTRICTED,
    ]
    assert [_SENSITIVITY_RANK[level] for level in ordered] == [0, 1, 2, 3]
    assert sorted(SensitivityLevel, key=lambda level: _SENSITIVITY_RANK[level]) == ordered


def test_lattice_order_differs_from_alphabetical_str_order() -> None:
    # Regression guard: the StrEnum's inherited ``<`` is alphabetical and WRONG;
    # governance order must come from the rank map, never from ``sorted(enum)``.
    by_rank = sorted(SensitivityLevel, key=lambda level: _SENSITIVITY_RANK[level])
    by_str = sorted(SensitivityLevel)
    assert by_rank != by_str
    assert by_str[0] == SensitivityLevel.CONFIDENTIAL  # alphabetical, not the bottom


# --- lub algebra ------------------------------------------------------------


def test_lub_takes_higher_level() -> None:
    a = Sensitivity(level=SensitivityLevel.INTERNAL)
    b = Sensitivity(level=SensitivityLevel.RESTRICTED)
    assert a.lub(b).level == SensitivityLevel.RESTRICTED


def test_lub_unions_compartments() -> None:
    a = Sensitivity(level=SensitivityLevel.PUBLIC, compartments=frozenset({"eu"}))
    b = Sensitivity(level=SensitivityLevel.INTERNAL, compartments=frozenset({"export"}))
    result = a.lub(b)
    assert result.level == SensitivityLevel.INTERNAL
    assert result.compartments == frozenset({"eu", "export"})


def test_lub_is_commutative() -> None:
    a = Sensitivity(level=SensitivityLevel.CONFIDENTIAL, compartments=frozenset({"x"}))
    b = Sensitivity(level=SensitivityLevel.INTERNAL, compartments=frozenset({"y"}))
    assert a.lub(b) == b.lub(a)


def test_lub_is_idempotent() -> None:
    a = Sensitivity(level=SensitivityLevel.CONFIDENTIAL, compartments=frozenset({"x"}))
    assert a.lub(a) == a


def test_lub_is_associative() -> None:
    a = Sensitivity(level=SensitivityLevel.PUBLIC, compartments=frozenset({"a"}))
    b = Sensitivity(level=SensitivityLevel.CONFIDENTIAL, compartments=frozenset({"b"}))
    c = Sensitivity(level=SensitivityLevel.INTERNAL, compartments=frozenset({"c"}))
    assert a.lub(b).lub(c) == a.lub(b.lub(c))


# --- flatten ----------------------------------------------------------------


def test_flatten_keys_and_types() -> None:
    s = Sensitivity(
        level=SensitivityLevel.RESTRICTED,
        compartments=frozenset({"export", "eu"}),
    )
    flat = s.flatten()
    assert flat == {
        "sensitivity_level": "restricted",
        "sensitivity_compartments": ["eu", "export"],  # sorted
    }
    assert isinstance(flat["sensitivity_level"], str)
    assert isinstance(flat["sensitivity_compartments"], list)


def test_flatten_default_is_public_empty() -> None:
    assert Sensitivity().flatten() == {
        "sensitivity_level": "public",
        "sensitivity_compartments": [],
    }


# --- defaults ---------------------------------------------------------------


def test_sensitivity_default_is_lattice_bottom() -> None:
    s = Sensitivity()
    assert s.level == SensitivityLevel.PUBLIC
    assert s.compartments == frozenset()


def test_node_and_edge_defaults_seed_public_sensitivity() -> None:
    doc = Document(id=uuid.uuid4())
    span = Span(id=uuid.uuid4(), document_id=doc.id, start=0, end=5)
    fact = Fact(
        id=uuid.uuid4(),
        box=uuid.uuid4(),
        tier=Tier.CASE,
        statement="x",
        annotations=_annotations(),
        temporal=_temporal(),
    )
    edge = EvidentialEdge(
        source=uuid.uuid4(),
        target=uuid.uuid4(),
        box=uuid.uuid4(),
        sign=EdgeSign.SUPPORTS,
        strength=0.7,
        significance=0.6,
        annotations=_annotations(),
        temporal=_temporal(),
    )
    for obj in (doc, span, fact, edge):
        assert obj.sensitivity == Sensitivity()


def test_box_interest_defaults_none_and_accepts_source_interest() -> None:
    base = dict(
        id=uuid.uuid4(),
        name="supplier-report",
        tier=Tier.REFERENCE,
        version="0.1.0",
        source="acme",
        reliability_prior=0.9,
        valid_from=_NOW,
    )
    assert Box(**base).interest is None  # None = unknown, distinct from empty stake
    interest = SourceInterest(role="component-supplier", stake=frozenset({"bearings"}))
    assert Box(**base, interest=interest).interest == interest


def test_source_interest_default_is_empty_known_stake() -> None:
    si = SourceInterest()
    assert si.role is None
    assert si.stake == frozenset()


# --- immutability / hashability ---------------------------------------------


def test_sensitivity_is_frozen() -> None:
    s = Sensitivity()
    with pytest.raises(ValidationError):
        s.level = SensitivityLevel.RESTRICTED  # type: ignore[misc]


def test_sensitivity_is_hashable() -> None:
    a = Sensitivity(level=SensitivityLevel.INTERNAL, compartments=frozenset({"x"}))
    b = Sensitivity(level=SensitivityLevel.INTERNAL, compartments=frozenset({"x"}))
    assert len({a, b}) == 1  # value-equal, hashable via frozen + frozenset


def test_source_interest_is_frozen_and_hashable() -> None:
    si = SourceInterest(role="auditor")
    with pytest.raises(ValidationError):
        si.role = "supplier"  # type: ignore[misc]
    assert len({si, SourceInterest(role="auditor")}) == 1
