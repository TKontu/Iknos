"""Unit tests for the pure stakes-gated quarantine gate (R9; §3.1).

DB-free and importable without ``DATABASE_URL`` — the gate is a total function over scalars. Covers
the three-row truth table (HIGH+reasons → raise, HIGH+empty → pass, LOW+any → pass), the error's
carried reasons, and the ``Stakes`` value strings.
"""

import pytest

from iknos.core.quarantine import (
    QuarantinedPropositionError,
    Stakes,
    assert_not_quarantined,
)


def test_high_stakes_with_reasons_raises() -> None:
    with pytest.raises(QuarantinedPropositionError) as exc:
        assert_not_quarantined(["low_faithfulness"], Stakes.HIGH)
    # The offending reasons are carried for the caller's audit/triage record.
    assert exc.value.reasons == ("low_faithfulness",)
    assert "low_faithfulness" in str(exc.value)


def test_high_stakes_without_reasons_passes() -> None:
    # A confirmed (non-provisional) source may drive a high-stakes move.
    assert assert_not_quarantined([], Stakes.HIGH) is None


def test_low_stakes_always_passes() -> None:
    # Corroboration is low-stakes — a provisional source may still lend it.
    assert assert_not_quarantined(["low_faithfulness", "unresolved_reference"], Stakes.LOW) is None
    assert assert_not_quarantined([], Stakes.LOW) is None


def test_error_with_no_reasons_renders_placeholder() -> None:
    # Defensive: the message never collapses to an empty reasons clause.
    err = QuarantinedPropositionError([])
    assert err.reasons == ()
    assert "(none)" in str(err)


def test_stakes_value_strings() -> None:
    assert [s.value for s in Stakes] == ["low", "high"]
