"""The ensemble gate's **SYMBOLIC** channel producer — a clingo/ASP consistency check (W3, §8(d)).

The §7.2 ensemble gate (``core/ensemble_gate.py``) authorises a ``refuted`` flip only on agreement
across its channels; :data:`~iknos.core.ensemble_gate.DEFAULT_GATE` *requires* the **SYMBOLIC**
channel, whose producer had not existed — so the gate withheld **every** automated flip
(safe-by-default, but the differentiator capability was silently non-functional). This module is
that producer, decided eyes-open (W3, 2026-06-11 architecture assessment, P3): the option-(a)
**minimal clingo consistency check over the affected sub-region**, the G4.5 symbolic-channel slice
pulled forward to unblock ``DEFAULT_GATE`` as designed.

**What the symbolic channel decides (§8(d), §8 *Tooling*).** Distinct from the QBAF (which is the
*gradual* adjudication being gated, §8(b)), the symbolic check asks the **logical** question: are
the hypothesis and the refuting claim *actually* inconsistent under the box's logic — a genuine
``P ∧ ¬P`` (mutual exclusion / polarity opposition), possibly *transitively* through the box's
derivation rules — or did the LLM assert a contradiction the logic does not bear out?

- **AFFIRM** — asserting the hypothesis **and** the refuter together is **UNSAT** (no stable model):
  the refutation is a real logical contradiction. The channel agrees the refutation holds.
- **DISSENT** — the two are logically **related** (they share a claim atom or a rule couples them)
  yet asserting them together is **SAT**: the refuter is *consistent* with the hypothesis, so the
  asserted contradiction is not borne out. A dissent **vetoes** under every gate policy (§13) — the
  guard the channel exists for ("LLM proposes, engine disposes").
- **ABSTAIN** — there is **no applicable logical signal**: the refuter shares no atom/rule with the
  sub-region (a semantic refutation the symbolic encoding cannot see — the LLM channel's job), or
  the sub-region is *already* inconsistent without the refuter (so the contradiction cannot be
  attributed to the refutation). Abstention is honest *insufficiency*, never a disguised dissent.

**Pure engine, DB at the edges (the codebase's discipline).** This is the analogue of
``core/qbaf.py`` / ``core/subjective_logic.py``: a pure decision core over a **typed query**
(:class:`SymbolicQuery`) — hand-buildable, unit-testable, no AGE. The atom **identity** (which
propositions are "the same claim", so opposite polarity is a ``P``/``¬P`` twin) is the embedding
twin-cluster the perception layer already computes (``core/consistency.py`` G1.14); the DB adapter
that reads a hypothesis + its candidate refuter out of the active sub-region and assigns those
cluster keys is the **consuming seam** (the ``find-contradiction`` operator, a later G4.5 slice).
This producer fixes the channel's *contract + engine* the way G4.5 slice 1 fixed the gate's decision
algebra before it was wired to AGE; a caller passes the produced :class:`~iknos.core.ensemble_gate.
ChannelSignal` straight into :func:`~iknos.core.ensemble_gate.authorise_from_panel`'s ``symbolic=``.

**Why clingo and not a set check.** A two-atom polarity clash *is* just a set test — but the
contradiction may be **transitive**: the refuter negates a base atom that the box's rules derive a
hypothesis-supporting atom from. Encoding the sub-region's rules as ASP and asking clingo for a
stable model decides those closure cases correctly (and stays correct as the encoding grows), which
is exactly the "symbolic consistency check over the affected sub-region" §8 *Tooling* names.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from iknos.core.ensemble_gate import (
    ChannelSignal,
    GateChannel,
    abstaining,
    affirming,
    dissenting,
)


@dataclass(frozen=True)
class Atom:
    """One logical claim atom: a claim **identity** (``key``) plus its **polarity**.

    Two atoms with the same ``key`` and opposite ``positive`` are a ``P`` / ``¬P`` pair — the
    polarity twin (G1.14). ``key`` is the caller's claim-identity token (the embedding twin-cluster
    id when wired to the graph); it is treated opaquely here — only equality of keys matters, so the
    engine maps each distinct key to an integer before grounding (no ASP string-escaping, no
    injection surface).
    """

    key: str
    positive: bool = True

    def negated(self) -> "Atom":
        """The polarity twin of this atom (same claim, opposite sign)."""
        return Atom(key=self.key, positive=not self.positive)


@dataclass(frozen=True)
class Rule:
    """A box derivation rule ``head ← body₁ ∧ … ∧ bodyₙ`` over :class:`Atom`s (a Horn clause).

    Lets the consistency check close *transitively*: a refuter that negates a body atom can make the
    head — and anything asserting the head's twin — inconsistent. An empty ``body`` is an axiom
    (the head holds unconditionally). The head/body polarities are honoured verbatim (a rule may
    derive a negated literal).
    """

    head: Atom
    body: tuple[Atom, ...] = ()


@dataclass(frozen=True)
class SymbolicQuery:
    """The affected sub-region for one ``(refuter → hypothesis)`` symbolic consistency check.

    All DB-free, hand-buildable:

    - ``hypothesis`` — the atoms the hypothesis asserts (its statement claim + the claims of its
      well-founded supporters).
    - ``refuter`` — the atoms the refuting evidence asserts (the proposed contradiction's content).
    - ``context`` — other active, well-founded base claims in the box (so a transitive clash can
      involve a third fact); optional.
    - ``rules`` — the box's derivation rules within the sub-region (optional).

    The check is *attributive*: it asks whether adding ``refuter`` to the (already-consistent)
    ``hypothesis ∪ context`` under ``rules`` introduces a contradiction.
    """

    hypothesis: tuple[Atom, ...]
    refuter: tuple[Atom, ...]
    context: tuple[Atom, ...] = ()
    rules: tuple[Rule, ...] = field(default_factory=tuple)


class Consistency(StrEnum):
    """The clingo check's verdict on a proposed refutation (maps 1:1 to a channel stance)."""

    CONTRADICTORY = "contradictory"  # hypothesis ∧ refuter is UNSAT — a real contradiction → AFFIRM
    CONSISTENT = "consistent"  # related but SAT — the refutation is not borne out → DISSENT
    UNRELATED = "unrelated"  # refuter shares no atom/rule with the sub-region → ABSTAIN
    INDETERMINATE = "indeterminate"  # sub-region already inconsistent w/o the refuter → ABSTAIN


@dataclass(frozen=True)
class ConsistencyResult:
    """The verdict plus a short audit detail (carried onto the :class:`ChannelSignal`, §10.1)."""

    verdict: Consistency
    detail: str


def _silent(_code: object, _message: str) -> None:
    """A no-op clingo logger so grounding/solve diagnostics never reach stderr (deterministic)."""


def _solve(facts: Iterable["_Lit"], rules: Iterable[tuple["_Lit", tuple["_Lit", ...]]]) -> bool:
    """Ground + solve the polarity-consistency program; return whether it is **satisfiable**.

    The program: one ``holds(id, t|f)`` fact per asserted literal, one Horn rule per derivation, and
    the single integrity constraint ``:- holds(K, t), holds(K, f)`` (a claim cannot be both asserted
    and denied). Atom keys are pre-mapped to integers by the caller, so the emitted text is closed
    constants only — no string escaping, no injection. clingo is imported lazily (the one heavy
    dependency) so importing this module stays cheap.
    """
    import clingo

    lines: list[str] = [":- holds(K, t), holds(K, f)."]
    for lit in facts:
        lines.append(f"holds({lit.aid}, {lit.pol}).")
    for head, body in rules:
        if body:
            conj = ", ".join(f"holds({b.aid}, {b.pol})" for b in body)
            lines.append(f"holds({head.aid}, {head.pol}) :- {conj}.")
        else:
            lines.append(f"holds({head.aid}, {head.pol}).")

    ctl = clingo.Control(logger=_silent, message_limit=0)
    ctl.add("base", [], "\n".join(lines))
    ctl.ground([("base", [])])
    return bool(ctl.solve().satisfiable)


@dataclass(frozen=True)
class _Lit:
    """An :class:`Atom` interned to an int id (``aid``) with its ASP polarity token (``pol``)."""

    aid: int
    pol: str  # "t" (asserted) or "f" (negated)


class _Interner:
    """Maps distinct atom keys → stable int ids (so ASP carries closed constants, not strings)."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}

    def lit(self, atom: Atom) -> _Lit:
        aid = self._ids.setdefault(atom.key, len(self._ids))
        return _Lit(aid=aid, pol="t" if atom.positive else "f")

    def has(self, key: str) -> bool:
        return key in self._ids


def check_consistency(query: SymbolicQuery) -> ConsistencyResult:
    """Decide whether ``query.refuter`` is a real logical contradiction of the hypothesis (W3).

    The *attributive* consistency test, generic over the sub-region's rules:

    1. **Baseline** — is ``hypothesis ∪ context`` (under ``rules``) already inconsistent *without*
       the refuter? If so the contradiction cannot be attributed to the refutation →
       :attr:`~Consistency.INDETERMINATE` (abstain).
    2. **Relatedness** — does any refuter atom share a claim key with the hypothesis/context atoms
       or any rule? If not, the symbolic encoding has nothing to say (a semantic refutation it can't
       see) → :attr:`~Consistency.UNRELATED` (abstain). This is the guard that keeps a trivially-SAT
       unrelated refuter from being mis-read as a dissent.
    3. **Attribution** — add the refuter and solve: **UNSAT** ⇒ :attr:`~Consistency.CONTRADICTORY`
       (affirm — a genuine ``P ∧ ¬P``, possibly transitive); **SAT** ⇒
       :attr:`~Consistency.CONSISTENT` (dissent — related but compatible, the refutation is not
       borne out).

    Pure (clingo is the only engine); deterministic. The single integrity constraint encodes "a
    claim cannot be both asserted and denied"; richer mutual-exclusion axioms slot in as more
    ``rules`` without changing this contract.
    """
    interner = _Interner()
    base_atoms = tuple(query.hypothesis) + tuple(query.context)
    base_lits = [interner.lit(a) for a in base_atoms]
    rule_lits = [
        (interner.lit(r.head), tuple(interner.lit(b) for b in r.body)) for r in query.rules
    ]

    # 1 — the sub-region must be consistent on its own for the refuter to be the attributable cause.
    if not _solve(base_lits, rule_lits):
        return ConsistencyResult(
            Consistency.INDETERMINATE,
            "sub-region already inconsistent without the refuter — contradiction not attributable",
        )

    # 2 — relatedness: the refuter must touch a claim the sub-region already mentions.
    #     `interner.has` is true for every key seen while interning the base atoms + rules above.
    related = any(interner.has(a.key) for a in query.refuter)
    if not related:
        return ConsistencyResult(
            Consistency.UNRELATED,
            "refuter shares no claim atom or rule with the sub-region — no symbolic signal",
        )

    # 3 — attribute: does adding the refuter break consistency?
    refuter_lits = [interner.lit(a) for a in query.refuter]
    if _solve(base_lits + refuter_lits, rule_lits):
        return ConsistencyResult(
            Consistency.CONSISTENT,
            "hypothesis and refuter are logically consistent — refutation not borne out",
        )
    return ConsistencyResult(
        Consistency.CONTRADICTORY,
        "hypothesis and refuter are mutually inconsistent (UNSAT) — contradiction confirmed",
    )


#: Maps a :class:`Consistency` verdict to the gate stance it produces (the channel's whole job).
_VERDICT_STANCE = {
    Consistency.CONTRADICTORY: affirming,
    Consistency.CONSISTENT: dissenting,
    Consistency.UNRELATED: abstaining,
    Consistency.INDETERMINATE: abstaining,
}


def symbolic_channel_for(query: SymbolicQuery) -> ChannelSignal:
    """Produce the **SYMBOLIC** :class:`~iknos.core.ensemble_gate.ChannelSignal` for a sub-region.

    Runs :func:`check_consistency` and maps its verdict to the channel stance (CONTRADICTORY →
    AFFIRM, CONSISTENT → DISSENT, UNRELATED/INDETERMINATE → ABSTAIN), carrying the clingo detail for
    the audit trail. This is the drop-in replacement for
    :func:`~iknos.core.ensemble_gate.symbolic_channel`'s ABSTAIN seam: a consumer that has built the
    sub-region passes the result as ``authorise_from_panel(..., symbolic=symbolic_channel_for(q))``,
    and :data:`~iknos.core.ensemble_gate.DEFAULT_GATE` is unblocked exactly as designed (§7.2).
    """
    result = check_consistency(query)
    return _VERDICT_STANCE[result.verdict](GateChannel.SYMBOLIC, result.detail)
