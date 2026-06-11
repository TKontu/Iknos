"""Stakes-gated quarantine of provisional atoms (G2.9; architecture §3.1, §7.2, §8).

The §3.1 rule, made enforceable: a **provisional** atom — a proposition the perception layer
could not verify above the faithfulness threshold (:func:`~iknos.types.epistemic.is_provisional`),
an ambiguously-bound mention's dependent proposition, or a defeasible inductive conclusion —
*"may exist but must not drive a strong move (e.g. a ``REFUTES`` that overturns a hypothesis) until
confirmed"* (§3.1, the same logic as the §7.2 ensemble gate on refutation). Until enforced the
``provisional`` flag is decorative (the Phase-2 entry-criterion's words); this module is the one
place that decision lives, so the edge producer (which *marks* an edge quarantined at creation,
§10) and the QBAF adapter (which *honours* the mark by not letting a quarantined edge drive
adjudication) cannot diverge on what "high-stakes" means.

**Pure / DB-free**, like :mod:`~iknos.types.epistemic` and the §11.2 verdict bands: the policy is a
swappable data object and the decision a total function over scalars, unit-testable without a
graph. The producer resolves an evidence node's provisional status from the graph (Fact →
``EVIDENCED_BY`` → ``Proposition.provisional``, or a conclusion's own ``provisional``) and feeds it
here; this module never touches AGE.

**What "high-stakes" means (the MVP, and its calibration seam).** §3.1 names the move
categorically — a ``REFUTES`` that overturns a hypothesis — so the MVP gates exactly the
categorical sign: a provisional source may *corroborate* (a ``SUPPORTS`` is a low-stakes move whose
provisional nature is already reflected in the edge's strength/the node's confidence) but may not
*refute*. §3.1 also says the cutoff is **stakes-dependent** ("a reference feeding a high-
significance refutation needs higher confidence than one feeding a minor corroboration") — a
*continuous* faithfulness-vs-significance threshold. That continuous form is a genuine calibration
question (Trial A5 / G4.6): it is left as the documented seam below rather than guessed here, while
the categorical rule — the one §3.1 states outright and the one that actually protects hypothesis
state — ships now. Swapping the seam in re-points the gate without touching either consumer, the
same *policy-as-data* discipline as :data:`~iknos.types.intentional._BAND_LOWER_BOUNDS` and
:data:`~iknos.core.edge_producer.DEFAULT_SIGNIFICANCE`.
"""

from dataclasses import dataclass, field

from iknos.types.edges import EdgeSign

# The §3.1 high-stakes move(s) a provisional atom may not drive. A frozenset (not a single sign)
# so the stakes vocabulary is swappable data: should a future calibration find that a provisional
# source must also not drive, say, a high-significance SUPPORTS, that is a one-line policy change,
# not a control-flow edit. REFUTES is the move §3.1 names ("a REFUTES that overturns a hypothesis").
_DEFAULT_HIGH_STAKES_SIGNS: frozenset[EdgeSign] = frozenset({EdgeSign.REFUTES})


@dataclass(frozen=True)
class QuarantinePolicy:
    """Which evidential moves a provisional source may not drive (§3.1) — swappable data.

    ``high_stakes_signs`` is the set of :class:`~iknos.types.edges.EdgeSign` directions that count
    as a *strong move*; a provisional source driving one of them is quarantined. Defaults to
    ``{REFUTES}`` — the move §3.1 names. A ``SUPPORTS`` is deliberately absent: corroboration is a
    low-stakes move, and a provisional source's weaker support is already expressed by its lower
    edge ``strength`` / node ``confidence``, not by a hard gate.

    **Calibration seam (deferred, §3.1 / G4.6).** The stakes-dependent *continuous* threshold —
    quarantine when ``faithfulness < f(significance)``, so a high-significance refutation demands
    higher source faithfulness than a minor one — is not yet fitted (Trial A5). When it lands it
    becomes an additional field here (e.g. a ``significance_to_min_faithfulness`` map) and a richer
    :func:`is_quarantined` signature; the categorical sign gate stays the conservative backstop.
    """

    high_stakes_signs: frozenset[EdgeSign] = field(
        default_factory=lambda: _DEFAULT_HIGH_STAKES_SIGNS
    )

    def is_high_stakes(self, sign: EdgeSign) -> bool:
        """Whether ``sign`` is a strong move a provisional source may not drive (§3.1)."""
        return sign in self.high_stakes_signs


DEFAULT_QUARANTINE = QuarantinePolicy()
"""The MVP policy: a provisional source may not drive a ``REFUTES`` (§3.1), but may ``SUPPORTS``."""


def is_quarantined(
    sign: EdgeSign,
    source_provisional: bool,
    *,
    policy: QuarantinePolicy = DEFAULT_QUARANTINE,
) -> bool:
    """Whether an evidential edge must be quarantined from driving adjudication (§3.1).

    ``True`` iff the edge's source atom is **provisional** *and* the edge's ``sign`` is a
    high-stakes move under ``policy`` (a ``REFUTES`` by default). A quarantined edge is still
    **written** to the graph — it may exist, carries its full provenance, and lifts automatically
    when the source is later confirmed (its ``provisional`` clears and a re-judgment overwrites the
    flag) — but the QBAF adapter drops it from the framework, so it lends nothing to a hypothesis's
    state until then (the §7.2-style gate, applied at the perception layer not the ensemble layer).

    Total over its scalar inputs and pure; ``source_provisional`` is the graph-resolved fact (a
    null ``provisional`` on the source reads as ``False`` — quarantine fires only on a *positive*
    provisional signal, never on absence of evidence).
    """
    return source_provisional and policy.is_high_stakes(sign)
