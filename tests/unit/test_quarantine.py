"""Unit tests for the stakes-gated quarantine policy (G2.9; §3.1).

DB-free: the policy is a pure data object and :func:`is_quarantined` a total function over scalars.
Covers the categorical MVP rule (a provisional source may not drive a ``REFUTES`` but may
``SUPPORTS``), the non-provisional pass-through, and a custom high-stakes-sign policy (the swappable
calibration seam).
"""

from iknos.core.quarantine import (
    DEFAULT_QUARANTINE,
    QuarantinePolicy,
    is_quarantined,
)
from iknos.types.edges import EdgeSign


def test_provisional_source_cannot_drive_a_refutes() -> None:
    # The §3.1 rule the MVP enforces: a provisional atom may not overturn a hypothesis.
    assert is_quarantined(EdgeSign.REFUTES, source_provisional=True) is True


def test_provisional_source_may_still_corroborate() -> None:
    # A SUPPORTS is a low-stakes move — a provisional source may lend it (its weakness is in
    # the edge strength / node confidence, not a hard gate).
    assert is_quarantined(EdgeSign.SUPPORTS, source_provisional=True) is False


def test_non_provisional_source_drives_any_sign() -> None:
    # Quarantine fires only on a *positive* provisional signal — never on its absence.
    assert is_quarantined(EdgeSign.REFUTES, source_provisional=False) is False
    assert is_quarantined(EdgeSign.SUPPORTS, source_provisional=False) is False


def test_default_policy_treats_only_refutes_as_high_stakes() -> None:
    assert DEFAULT_QUARANTINE.is_high_stakes(EdgeSign.REFUTES) is True
    assert DEFAULT_QUARANTINE.is_high_stakes(EdgeSign.SUPPORTS) is False


def test_custom_policy_can_extend_high_stakes_signs() -> None:
    # The swappable-data seam: a calibration that also gates high-stakes SUPPORTS is a policy edit.
    policy = QuarantinePolicy(high_stakes_signs=frozenset({EdgeSign.REFUTES, EdgeSign.SUPPORTS}))
    assert is_quarantined(EdgeSign.SUPPORTS, source_provisional=True, policy=policy) is True
    assert is_quarantined(EdgeSign.SUPPORTS, source_provisional=False, policy=policy) is False
