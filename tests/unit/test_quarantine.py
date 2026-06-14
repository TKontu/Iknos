"""Unit tests for the §3.1 stakes-gated quarantine gate (R9).

Pure: the gate has no DB and no settings, so this module imports and runs without
``DATABASE_URL`` (the V7 enforcement seam stays DB-bound; the decision does not). The core is
the three-row truth table — HIGH gates on any reason, everything else passes — plus the
normalisation contract (dedup/sort/empty) the gate shares with ``merge_provisional_reasons`` and
the structured cause the exception carries for the triage signal.
"""

import pytest

from iknos.core.quarantine import (
    QuarantinedPropositionError,
    Stakes,
    _gates_on_provisional,
    assert_not_quarantined,
)
from iknos.types.epistemic import ProvisionalReason, provisional_threshold_for

# --- the three-row truth table (§3.1, R9 accept) ---------------------------------------------


def test_high_stakes_with_reasons_raises() -> None:
    """HIGH + non-empty reasons → quarantined: a provisional atom must not drive a strong move."""
    with pytest.raises(QuarantinedPropositionError):
        assert_not_quarantined([ProvisionalReason.LOW_FAITHFULNESS], Stakes.HIGH)


def test_high_stakes_without_reasons_passes() -> None:
    """HIGH + no reasons → authorised: a fully-assessed atom may drive the strong move."""
    assert assert_not_quarantined([], Stakes.HIGH) is None


def test_low_stakes_always_passes() -> None:
    """LOW always passes — even a provisional atom may exist and corroborate (§3.1)."""
    assert assert_not_quarantined([ProvisionalReason.LOW_FAITHFULNESS], Stakes.LOW) is None


def test_low_stakes_without_reasons_passes() -> None:
    """The fourth cell for completeness: LOW + no reasons is trivially authorised."""
    assert assert_not_quarantined([], Stakes.LOW) is None


# --- normalisation contract (shared with merge_provisional_reasons) --------------------------


def test_accepts_plain_strings_not_only_enum() -> None:
    """Reasons arrive as persisted ``list[str]`` (decode_provisional_reasons), not enum members."""
    with pytest.raises(QuarantinedPropositionError):
        assert_not_quarantined(["unresolved_reference"], Stakes.HIGH)


def test_mixed_enum_and_string_reasons_gate() -> None:
    """A producer's OR-folded union may mix enum members and decoded strings; both gate."""
    with pytest.raises(QuarantinedPropositionError):
        assert_not_quarantined(
            [ProvisionalReason.LOW_FAITHFULNESS, "unresolved_reference"], Stakes.HIGH
        )


def test_reasons_on_exception_are_deduped_and_sorted() -> None:
    """The carried cause is normalised (dedup + sort) so the triage signal is order-stable."""
    with pytest.raises(QuarantinedPropositionError) as exc:
        assert_not_quarantined(
            ["unresolved_reference", "low_faithfulness", "low_faithfulness"], Stakes.HIGH
        )
    assert exc.value.reasons == ("low_faithfulness", "unresolved_reference")
    assert exc.value.stakes is Stakes.HIGH


def test_exception_message_lists_the_reasons() -> None:
    """The message names the reasons (R9: 'message lists reasons') for a legible triage log."""
    with pytest.raises(QuarantinedPropositionError) as exc:
        assert_not_quarantined([ProvisionalReason.POLARITY_UNSTABLE], Stakes.HIGH)
    assert "polarity_unstable" in str(exc.value)


def test_empty_set_input_passes_at_high_stakes() -> None:
    """A set (not just a list) with no reasons is 'not provisional' — falsy collection, no raise."""
    assert assert_not_quarantined(set(), Stakes.HIGH) is None


# --- the gate is a property of the move, not the atom (§3.1) ----------------------------------


def test_same_reasons_gate_high_but_not_low() -> None:
    """The defining asymmetry: identical reasons quarantine the HIGH move yet pass the LOW one."""
    reasons = [ProvisionalReason.LOW_FAITHFULNESS]
    assert assert_not_quarantined(reasons, Stakes.LOW) is None
    with pytest.raises(QuarantinedPropositionError):
        assert_not_quarantined(reasons, Stakes.HIGH)


# --- the gate's stakes-dependence is derived from the threshold function (G1.6) ---------------


@pytest.mark.parametrize("stakes", list(Stakes))
def test_gating_derives_from_the_stakes_threshold(stakes: Stakes) -> None:
    """A stakes level gates iff it sets a non-zero faithfulness bar — the gate and the floor read
    the *same* stakes-dependent threshold (epistemic.provisional_threshold_for), not a separate
    hand-maintained boolean. This is what makes the threshold the single source of truth for both
    'is this atom provisional' and 'does that matter for this move'."""
    assert _gates_on_provisional(stakes) is (provisional_threshold_for(stakes) > 0.0)


def test_high_gates_low_does_not() -> None:
    """The concrete two-level reading of the threshold today: HIGH (strict bar) gates, LOW (0.0,
    permissive) does not."""
    assert _gates_on_provisional(Stakes.HIGH) is True
    assert _gates_on_provisional(Stakes.LOW) is False
