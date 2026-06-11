"""The stakes-gated quarantine of provisional atoms (R9; architecture §3.1, §7.2, §8).

The §3.1 rule, made enforceable: a **provisional** atom — a proposition the perception layer could
not verify above the faithfulness threshold, an ambiguously-bound mention's dependent proposition,
or a budget-capped inductive conclusion (the :class:`~iknos.types.epistemic.ProvisionalReason`
causes carried by R8) — *"may exist but must not drive a strong move (e.g. a ``REFUTES`` that
overturns a hypothesis) until confirmed"*. This module is the **one pure gate** that decision flows
through, so every write site agrees on what "high-stakes" means and a provisional atom cannot
silently drive a refutation.

**The call contract (every high-stakes write site obeys it).** Before persisting an evidential
edge, the caller derives the move's :class:`Stakes` and calls :func:`assert_not_quarantined` with
the **source atom's** provisional reasons:

- a ``REFUTES`` edge is **always** ``Stakes.HIGH`` — overturning a hypothesis is the §3.1 strong
  move;
- a ``SUPPORTS`` edge is ``Stakes.HIGH`` *iff* it would be the target hypothesis's **sole** support
  (a lone provisional supporter would carry the hypothesis on its own — also a strong move), and
  ``Stakes.LOW`` otherwise (corroboration: a provisional source's weaker contribution is already
  reflected in the edge ``strength`` / node ``confidence``, no hard gate needed).

The first (and so far only) caller is the Phase-4 edge producer (V7), which catches the raise,
**drops** the edge from its plan, and records it on the producing ``Action`` as a triage signal —
the edge is never persisted and never a silent skip. The gate itself is **pure** (no DB, no
settings, importable without ``DATABASE_URL``): it decides, it does not read the graph or write the
audit — those are the caller's.

**Why raise rather than return a bool.** A quarantined high-stakes write is a contract violation at
the call site, not a value to thread through; raising forces every creation path to handle it
explicitly (drop + record) rather than silently coercing a forgotten check into "allowed". The
caller turns the exception into a recorded drop; an *unhandled* raise is a real bug (a high-stakes
write site that never considered quarantine).
"""

from collections.abc import Collection
from enum import StrEnum


class QuarantinedPropositionError(Exception):
    """A provisional atom was about to drive a high-stakes move (§3.1) — raised by the gate.

    Carries the offending ``reasons`` so the caller can record *why* on the audit trail (the
    triage signal) rather than re-deriving them. Caught at the edge-creation site (V7), which drops
    the edge from the plan and records the drop; an *uncaught* instance is a high-stakes write path
    that forgot to gate — a bug, surfaced loudly.
    """

    def __init__(self, reasons: Collection[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(
            "provisional atom may not drive a high-stakes move (§3.1); reasons: "
            + (", ".join(self.reasons) or "(none)")
        )


class Stakes(StrEnum):
    """The stakes of an evidential move, gating how a provisional source may drive it (§3.1).

    ``HIGH`` — a ``REFUTES`` (overturns a hypothesis) or a sole-support ``SUPPORTS`` (carries a
    hypothesis on its own); a provisional source may **not** drive one. ``LOW`` — a corroborating
    ``SUPPORTS`` among others; always permitted (the provisional source's weakness lives in the
    edge ``strength``, not a hard gate).
    """

    LOW = "low"
    HIGH = "high"


def assert_not_quarantined(proposition_reasons: Collection[str], stakes: Stakes) -> None:
    """Raise if a provisional source would drive a high-stakes move (§3.1) — the pure gate.

    Raises :class:`QuarantinedPropositionError` **iff** ``stakes is Stakes.HIGH`` *and*
    ``proposition_reasons`` is non-empty (the source carries at least one
    :class:`~iknos.types.epistemic.ProvisionalReason`). ``Stakes.LOW`` always passes; an empty
    reason set (a non-provisional / fully-confirmed source) always passes. The full truth table:

    ====== ================ ========
    stakes reasons          result
    ====== ================ ========
    HIGH   non-empty        **raise**
    HIGH   empty            pass
    LOW    any              pass
    ====== ================ ========

    Pure: it neither reads the graph nor writes the audit — the caller resolves the source's reasons
    and records any drop. ``proposition_reasons`` is the union of the
    :class:`~iknos.types.epistemic.ProvisionalReason` *values* over the source atom's provenance
    (the edge producer folds them over a Fact's ``EVIDENCED_BY`` ``Proposition``s).
    """
    if stakes is Stakes.HIGH and proposition_reasons:
        raise QuarantinedPropositionError(proposition_reasons)
