"""The §3.1 stakes-gated quarantine — the pure gate over provisional propositions (R9).

§3.1 ("Leave uncertainty open; gate by stakes") decides that a proposition below the calibrated
binding/faithfulness threshold is **marked provisional and quarantined from high-stakes downstream
use** — "it may exist but must not drive a strong move (e.g., a ``REFUTES`` that overturns a
hypothesis) until confirmed." R8 turned that single ``provisional`` boolean into a **set of
reasons** (:class:`~iknos.types.epistemic.ProvisionalReason`) so triage (§11.1) can act on *why*;
this module is the other half — the **gate** that reads only whether the set is non-empty, and only
at the stakes where the spec says it matters.

It is the deliberate analogue of ``core/ensemble_gate.py``: the **pure decision** with no DB, no
AGE, no settings, no LLM — a value algebra unit-testable in isolation. The *enforcement* (loading a
node's reasons, deriving the per-edge stakes, recording the dropped edge as a triage signal) is V7
in ``core/edge_producer.py``; this module fixes only the authorisation contract V7 calls.

**The call contract (V7, §7.2/§8).** Stakes are **per would-be move**, not a property of the
proposition. Every path that writes a high-stakes edge must call :func:`assert_not_quarantined` with
``Stakes.HIGH`` *before persisting*, passing the union of provisional reasons over the evidence the
edge rests on. High-stakes moves are:

- any ``REFUTES`` edge — refutation retracts downstream conclusions (§7.3) and is the bias-prone
  direction the whole non-monotonic layer exists to discipline (§7.2, §8); a wrong sign is
  catastrophic (§8). A provisional atom must never drive one.
- a ``SUPPORTS`` edge that would be its target hypothesis's **sole support** in the plan — a single
  provisional supporter is load-bearing; the same atom may still drive a ``SUPPORTS`` that merely
  adds to existing support (``Stakes.LOW``), because there the graph does not hinge on it.

``Stakes.LOW`` **always passes**: provisional propositions are allowed to *exist* and to participate
in low-stakes corroboration; quarantine gates the strong move, it does not delete the atom. On a
raise the caller does **not** abort the batch — it drops that one edge from the plan and records
``{evidence_id, sign, reasons, stakes}`` as a triage signal (§11.1), exactly as the ensemble gate
surfaces a withheld flip as a finding rather than an error (§13).
"""

from collections.abc import Collection

from iknos.types.epistemic import Stakes, merge_provisional_reasons, provisional_threshold_for

# ``Stakes`` is the move-stakes vocabulary; it lives in ``types/epistemic`` alongside the
# stakes → faithfulness-bar mapping it keys (so the threshold has one home and no ``core`` →
# ``types`` layering inversion), and is re-exported here because this gate and the enforcement
# seam (``core/edge_producer`` V7) are its callers. Re-exported, not redefined — one Stakes only.
__all__ = ["QuarantinedPropositionError", "Stakes", "assert_not_quarantined"]


def _gates_on_provisional(stakes: Stakes) -> bool:
    """Whether a stakes level gates on provisional reasons at all (§3.1, G1.6).

    Derived from the single stakes-dependent threshold (:func:`provisional_threshold_for`): a level
    gates iff it sets a non-zero faithfulness bar. So this gate and the proposition-time provisional
    floor read the **same** source of truth — recalibrating the threshold (or adding a future middle
    band) moves both at once, and an unmapped stakes level raises a KeyError there (fail-loud on
    vocabulary growth) rather than silently defaulting to "passes", which for a *safety* gate is the
    dangerous default.

    Today this is two-level and **exact**: ``HIGH`` sets the 0.5 floor that *defines* the
    ``LOW_FAITHFULNESS`` reason, so "any reason at HIGH" ⟺ "faithfulness below the HIGH bar", and
    ``LOW``'s 0.0 bar means it never gates. A *graded* middle band (a bar strictly between 0 and the
    proposition-time floor) would need the faithfulness value carried to the gate, not just the
    precomputed reason set; the Stakes enum and the threshold map are positioned so that lands as a
    localized change when a trial calibrates the spectrum.
    """
    return provisional_threshold_for(stakes) > 0.0


class QuarantinedPropositionError(Exception):
    """A provisional proposition was about to drive a high-stakes move (§3.1, R9).

    Raised by :func:`assert_not_quarantined` so the would-be edge is dropped from the plan and
    routed to triage, never persisted. Carries the structured cause — :attr:`reasons` (the
    normalised, deduped, sorted provisional reasons) and :attr:`stakes` — so the caller (V7) can
    record the triage signal ``{evidence_id, sign, reasons, stakes}`` straight off the exception
    rather than re-deriving it. **Not an error condition to abort on**: it is the gate firing as
    designed, a §11.1 finding the caller surfaces.
    """

    def __init__(self, *, reasons: Collection[str], stakes: Stakes) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        self.stakes = stakes
        super().__init__(
            f"proposition is quarantined from {stakes} moves by provisional reason(s) "
            f"{list(self.reasons)} (§3.1): it may exist but must not drive a strong move "
            f"(a REFUTES, or a sole-support SUPPORTS) until confirmed via expert triage (§11.1)."
        )


def assert_not_quarantined(proposition_reasons: Collection[str], stakes: Stakes) -> None:
    """Gate a would-be graph move on the evidence's provisional reasons (§3.1, R9). Pure.

    The whole truth table: ``Stakes.HIGH`` **and** any provisional reason present → raise
    :class:`QuarantinedPropositionError` (the message lists the reasons); every other case — ``LOW``
    at any reasons, or ``HIGH`` with no reasons — returns ``None`` (the move is authorised). ``LOW``
    always passes by design: a provisional atom may exist and corroborate; quarantine gates only the
    strong move (see the module call contract).

    ``proposition_reasons`` is the **union** of provisional reasons over the evidence the move rests
    on — the caller OR-folds them (a Fact inherits the union over the ``Proposition``s it is
    ``EVIDENCED_BY``, V7). It is normalised through
    :func:`~iknos.types.epistemic.merge_provisional_reasons` (the one source of truth for the
    reason-list shape) so emptiness, dedup, and ordering match what was persisted; a falsy/empty
    collection is "no reason → not provisional", never an error.
    """
    reasons = merge_provisional_reasons(proposition_reasons)
    if _gates_on_provisional(stakes) and reasons:
        raise QuarantinedPropositionError(reasons=reasons, stakes=stakes)
