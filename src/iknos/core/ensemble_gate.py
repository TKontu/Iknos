"""The §7.2 ensemble gate — the refuted-flip authoriser (Phase 4, G4.5 slice 1).

A `Hypothesis`'s state is **computed**, never hand-set: the QBAF (``core/qbaf.py``) reads off a
*structural* ``refuted`` when net attack out-argues net support (``classify_state``). But §7.2 is
emphatic that this structural finding is **not by itself a licence to persist a flip**: "a flip to
``refuted`` requires the **ensemble gate** (multi-sample LLM + symbolic + temporal agreement),
never a single judgment." Refutation is the high-stakes, bias-prone direction (a wrong sign is
catastrophic, §8) and the one the whole non-monotonic layer exists to discipline — so it gets its
own authoriser. This module is that authoriser: the **pure decision algebra** over the three
channels, the Phase-4 analogue of how ``core/qbaf.py`` is the pure adjudication core and
``core/subjective_logic.py`` the pure scoring core — no DB, no AGE, no LLM, no migration, a small
value algebra unit-testable in isolation.

**The three channels (§6, §8 staged-build step 6).** ``find-contradiction`` "requires agreement
across multi-sample LLM judgment, a symbolic consistency check, and (where time matters) a temporal
check before it may assert a ``refutes`` edge." Each is a :class:`GateChannel`:

- **LLM** — the blind, randomized, multi-sample :class:`~iknos.core.edge_judge.EdgeJudge` panel
  (G4.3). Did the panel *agree* on a refutation, or did it split direction (the
  ``sign_stable=False`` finding the producer surfaces, §13)? This module's :func:`llm_channel`
  derives this channel's stance from the produced :class:`~iknos.core.edge_judge.EdgeJudgment`s —
  the one channel already computable from the shipped pipeline.
- **SYMBOLIC** — a logical-consistency check (§8 Tooling names **clingo / ASP**) that the refuting
  claim and the hypothesis are *actually* inconsistent (mutual exclusion / polarity opposition),
  guarding against the LLM asserting a contradiction the logic does not bear out. This is **not**
  the QBAF (the QBAF is the gradual adjudication *being gated*, §8(b)) — it is the separate symbolic
  check of §8(d) / Tooling. **Its producer landed (W3): ``core/symbolic_gate.py`` runs a real clingo
  consistency check over the affected sub-region;** :func:`symbolic_channel` here remains the
  ABSTAIN seam a caller uses when it has *not* built that sub-region (the safe default).
- **TEMPORAL** — *where time matters*, a check that the timeline supports the contradiction (e.g.
  the refuting fact's validity window actually overlaps / overturns). Conditionally applicable: when
  time is irrelevant it ABSTAINs and is ignored; when it matters and forbids, it DISSENTs (veto).
  Its producer (the §7.4 bitemporal check) is a later slice; here it ABSTAINs by default.

**The gate policy is an explicit decision (a fixture, before the engine), not a default** — the same
G3.5 / G4.1 / G4.3-slice-1 discipline (``DEFAULT_SEMANTICS``, ``DEFAULT_FUSION`` were each decided
with a numeric fixture because the choice is epistemic, then the engine written generic over the
value). Two policies are in tension:

- **Majority vote** across channels — authorise if more channels affirm than dissent.
- **Unanimity-of-required with a universal dissent veto** — authorise iff every *required* channel
  affirms **and no channel dissents**; a single dissent is a §13 finding, never out-voted.

**Decision, recorded eyes-open: ``DEFAULT_GATE`` is the unanimity-of-required, dissent-vetoing
gate**, with ``{LLM, SYMBOLIC}`` required and ``TEMPORAL`` conditional. Refutation is irreversible-
in-spirit (it retracts downstream conclusions, §7.3) and the standing §13 risk is **correlated LLM
error the disciplines do not remove** — so the gate must *not* let a confident-but-wrong LLM channel
carry a flip past a dissenting symbolic or temporal check, exactly the failure a majority vote would
permit (the fixture in ``test_ensemble_gate.py`` shows majority authorising a flip one channel
vetoes; the conservative gate withholds it). This parallels every other Phase-4 choice: **default to
the policy that cannot inflate certainty; retain the looser one at the seam.** :data:`STRICT_GATE`
(all three required) and :data:`LLM_ONLY_GATE` (an MVP sub-domain acting on the LLM channel alone,
before the symbolic/temporal producers land) are retained seams, so the choice stays reversible.

**Safe-by-default while the ensemble is incomplete.** Because ``SYMBOLIC`` is *required* but its
producer does not exist yet (it ABSTAINs), ``DEFAULT_GATE`` **withholds every automated ``refuted``
flip until that producer lands** — and that is the *correct* reading of §7.2 + design principle 6
("symbolic state authoritative; LLM proposes, engine disposes"): until the full ensemble can speak,
a structural refutation is **surfaced as a finding for expert review**, never auto-persisted. A
withheld flip is therefore not an error — it is :attr:`GateDecision.is_finding`, the unresolved
region the caller presents (§13), exactly as the QBAF surfaces non-convergence. A deployment that
wants to act on the LLM channel alone before the other producers exist swaps in
:data:`LLM_ONLY_GATE` at the seam, eyes-open.

**What this slice is *not* (documented seams, the rest of G4.5).** The ``SYMBOLIC`` producer landed
(W3, ``core/symbolic_gate.py``); the remaining channel *producer* — the bitemporal check for
``TEMPORAL`` — and the *consumer* — the
``persist_verdicts`` filter that drops un-authorised ``refuted`` verdicts, and the
``find-contradiction`` / ``corroborate`` operators that feed candidates into the
``REFUTES → retract → A → B → QBAF`` body wired into ``core/composed_loop.py::stabilize`` (G3.9) —
are later slices. This slice fixes the **authorisation contract** (the decision algebra + the
channel value types + the LLM-channel bridge) the way G4.1 fixed the adjudication core before G4.4
wired it to AGE.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from iknos.core.edge_judge import EdgeJudgment
from iknos.core.truth_maintenance import NodeId
from iknos.types.edges import EdgeSign


class GateChannel(StrEnum):
    """One of the three independent agreement channels the ensemble gate fuses (§7.2, §6).

    Kept a closed enum (not free strings) so the policy's *required* set and the per-channel
    producers cannot drift apart, and so a duplicate/typo channel is a surfaced error rather than a
    silently-ignored signal.
    """

    LLM = "llm"  # the multi-sample blind/randomized edge-judge panel (G4.3)
    SYMBOLIC = "symbolic"  # the logical-consistency check (clingo/ASP, §8 Tooling)
    TEMPORAL = "temporal"  # the bitemporal-validity check, where time matters (§7.4)


class ChannelStance(StrEnum):
    """How one channel bears on the proposed ``refuted`` flip — a deliberate **three-way** stance.

    The third value is what makes the gate correct: a channel that *cannot speak* (the temporal
    check when time is irrelevant, or any producer not yet wired) must be distinguishable from one
    that *speaks against* the flip — collapsing the two would let an inapplicable channel either
    block everything or rubber-stamp everything. So:

    - :attr:`AFFIRM` — this channel **agrees** the refutation holds.
    - :attr:`DISSENT` — this channel has positive signal **against** the refutation (the evidence
      does not refute / the claims are logically consistent / the timeline forbids it). A dissent
      **vetoes** under every gate policy — it is a §13 finding, never out-voted.
    - :attr:`ABSTAIN` — this channel has **no applicable or sufficient** signal (not wired yet, not
      applicable, or too inconsistent to call — e.g. a sign-unstable panel). It neither affirms nor
      vetoes; a *required* channel that abstains simply fails to clear the bar (withhold), a
      non-required one is ignored.
    """

    AFFIRM = "affirm"
    DISSENT = "dissent"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class ChannelSignal:
    """One channel's verdict on the proposed flip, with an auditable reason (§10.1).

    ``detail`` is a short human/audit string ("panel split direction", "no clingo model", …) carried
    onto the :class:`GateDecision` so a withheld flip explains *which* channel withheld it and why —
    the surfaced finding, not a bare boolean. Frozen + value-typed, like every other Phase-4 record.
    """

    channel: GateChannel
    stance: ChannelStance
    detail: str = ""


def affirming(channel: GateChannel, detail: str = "") -> ChannelSignal:
    """A channel signal that **affirms** the refutation (convenience constructor)."""
    return ChannelSignal(channel=channel, stance=ChannelStance.AFFIRM, detail=detail)


def dissenting(channel: GateChannel, detail: str = "") -> ChannelSignal:
    """A channel signal that **dissents** (vetoes the refutation) — convenience constructor."""
    return ChannelSignal(channel=channel, stance=ChannelStance.DISSENT, detail=detail)


def abstaining(channel: GateChannel, detail: str = "") -> ChannelSignal:
    """A channel signal that **abstains** (no applicable signal) — convenience constructor.

    This is the default state of the ``SYMBOLIC`` and ``TEMPORAL`` channels until their producers
    land (later G4.5 slices): :func:`symbolic_channel` / :func:`temporal_channel` return exactly
    this, so the wiring contract is real (a present-but-abstaining channel), not absent.
    """
    return ChannelSignal(channel=channel, stance=ChannelStance.ABSTAIN, detail=detail)


class GateOutcome(StrEnum):
    """The gate's verdict on a proposed ``refuted`` flip (§7.2)."""

    AUTHORISED = "authorised"  # the ensemble agreed — the flip may be persisted
    WITHHELD = "withheld"  # the ensemble did not agree — surface as a finding, do not flip


@dataclass(frozen=True)
class GateDecision:
    """The outcome of :func:`authorise` — whether to persist a ``refuted`` flip, and why (§7.2/§13).

    ``signals`` is the per-channel breakdown the decision was made from (deterministically ordered
    by channel), and ``reasons`` the human-readable account of *why* a flip was withheld (the
    dissenting and the unmet-required channels) — empty when authorised. A
    :attr:`WITHHELD <GateOutcome.WITHHELD>` decision on a structurally-refuted hypothesis is
    :attr:`is_finding`: the unresolved region the
    caller surfaces for expert review (§13), never silently dropped *or* silently flipped.
    """

    outcome: GateOutcome
    signals: tuple[ChannelSignal, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def authorised(self) -> bool:
        """Whether the flip is authorised (the ensemble agreed)."""
        return self.outcome is GateOutcome.AUTHORISED

    @property
    def is_finding(self) -> bool:
        """Whether this is an unresolved region to surface (the flip was withheld) — §13.

        A withheld flip means the QBAF computed a structural ``refuted`` the ensemble would not
        authorise: the caller presents the conflict as unresolved (the hypothesis keeps its prior
        state) rather than smoothing it into a verdict in either direction.
        """
        return self.outcome is GateOutcome.WITHHELD


@dataclass(frozen=True)
class RefutationGate:
    """The gate policy as a **value** (mirroring ``GradualSemantics`` / ``Fusion``) — selected at a
    seam, not branched on.

    ``required`` is the set of channels that **must** affirm for an authorisation; every gate
    additionally **vetoes on any dissent** (that rule is not a policy knob — a dissent is a §13
    finding under all policies). So a gate is fully described by *which channels are mandatory*:

    - :data:`DEFAULT_GATE` — ``{LLM, SYMBOLIC}`` required, ``TEMPORAL`` conditional. The recorded
      Phase-4 default (see module docstring): the conservative, cannot-inflate choice.
    - :data:`STRICT_GATE` — all three required. For a time-sensitive sub-domain where a temporal
      check must always weigh in.
    - :data:`LLM_ONLY_GATE` — ``{LLM}`` required. The MVP/seam that acts on the multi-sample panel
      alone before the symbolic/temporal producers land; looser, so eyes-open.

    The policy carries ``name`` for the Action/audit trail. :func:`authorise` is written **once,
    generic over the gate**, so swapping the policy is a one-value change, not a re-implementation.
    """

    name: str
    required: frozenset[GateChannel]


#: The conservative Phase-4 default the fixture decided on: ``{LLM, SYMBOLIC}`` required, a dissent
#: vetoes. Until the symbolic producer lands this withholds every automated flip (safe-by-default —
#: the structural refutation is surfaced for expert review, §7.2 + principle 6). The default
#: :func:`authorise` argument, so the choice stays reversible at this seam.
DEFAULT_GATE = RefutationGate(
    name="default", required=frozenset({GateChannel.LLM, GateChannel.SYMBOLIC})
)

#: All three channels required — the strict variant for a time-sensitive sub-domain. Retained at the
#: seam, not the default (a temporal check is "where time matters", not universally, §6/§7.2).
STRICT_GATE = RefutationGate(
    name="strict",
    required=frozenset({GateChannel.LLM, GateChannel.SYMBOLIC, GateChannel.TEMPORAL}),
)

#: LLM channel only — the MVP/decorrelated-sub-domain seam that authorises on the multi-sample panel
#: alone (still dissent-vetoed) before the symbolic/temporal producers exist. Looser than the
#: default by design; a deployment opts into it explicitly. **Not** the default.
LLM_ONLY_GATE = RefutationGate(name="llm-only", required=frozenset({GateChannel.LLM}))


def authorise(
    signals: Iterable[ChannelSignal],
    *,
    gate: RefutationGate = DEFAULT_GATE,
) -> GateDecision:
    """Decide whether the ensemble authorises a ``refuted`` flip (§7.2) — the pure decision algebra.

    Called for a hypothesis the QBAF computed a *structural* ``refuted`` for (``classify_state``):
    the structural finding is the *input* to this gate, not a licence on its own. The rule, generic
    over ``gate``:

    1. **Any** :attr:`~ChannelStance.DISSENT` ⇒ :attr:`~GateOutcome.WITHHELD` (a veto under every
       policy — a channel speaking against the refutation is a §13 finding, never out-voted).
    2. Every channel in ``gate.required`` must be present and :attr:`~ChannelStance.AFFIRM`; a
       required channel that abstains (or is missing) ⇒ :attr:`~GateOutcome.WITHHELD` (the ensemble
       did not affirm — §7.2 "never a single judgment").
    3. Otherwise ⇒ :attr:`~GateOutcome.AUTHORISED`.

    A non-required channel that abstains is ignored; a non-required channel that *affirms* is
    welcome but not necessary. A missing required channel is treated as an abstention (it did not
    speak), not an error — the caller may legitimately not have run a producer yet — but a
    **duplicate** channel *is* an error (two contradictory signals for one channel is a caller bug,
    surfaced not silently resolved). Signals are ordered deterministically by channel (§10 replay).
    """
    by_channel: dict[GateChannel, ChannelSignal] = {}
    for sig in signals:
        if sig.channel in by_channel:
            raise ValueError(
                f"duplicate signal for channel {sig.channel!r} — one signal per channel "
                "(fuse multiple producers into a single channel stance before gating)"
            )
        by_channel[sig.channel] = sig

    ordered = tuple(by_channel[c] for c in GateChannel if c in by_channel)

    reasons: list[str] = []

    # Rule 1 — any dissent vetoes (under every policy). Collected across all channels, required or
    # not: a temporal/symbolic check speaking *against* the flip blocks it even if not mandatory.
    for sig in ordered:
        if sig.stance is ChannelStance.DISSENT:
            reasons.append(_reason(sig, "dissents"))

    # Rule 2 — every required channel must affirm; abstention (or absence) of a required channel
    # withholds. A channel that already dissented is not double-reported here.
    for channel in sorted(gate.required):
        req_sig = by_channel.get(channel)
        if req_sig is None:
            reasons.append(f"required channel {channel.value!r} did not report (abstain)")
        elif req_sig.stance is ChannelStance.ABSTAIN:
            reasons.append(_reason(req_sig, "is required but abstained"))
        # AFFIRM clears; DISSENT already recorded by rule 1.

    outcome = GateOutcome.WITHHELD if reasons else GateOutcome.AUTHORISED
    return GateDecision(outcome=outcome, signals=ordered, reasons=tuple(reasons))


def _reason(sig: ChannelSignal, verb: str) -> str:
    """A compact ``"<channel> <verb>[: <detail>]"`` audit reason for a withheld flip."""
    base = f"{sig.channel.value} {verb}"
    return f"{base}: {sig.detail}" if sig.detail else base


# --------------------------------------------------------------------------------------------
# Channel producers — the bridges from the pipeline to a channel stance.
#
# The LLM channel is derivable today from the shipped edge-judge output; the symbolic and temporal
# channels ABSTAIN until their producers land (later G4.5 slices). Each is one pure function so the
# gate's *consumers* (the persist_verdicts filter, the find-contradiction operator) call a stable
# contract regardless of which producers exist yet.
# --------------------------------------------------------------------------------------------


def llm_channel(
    judgments: Iterable[EdgeJudgment],
    *,
    hypothesis: NodeId | None = None,
) -> ChannelSignal:
    """Derive the **LLM** channel's stance from the multi-sample edge-judge panel (§8, G4.3).

    The LLM channel agrees a hypothesis is refuted iff the blind/randomized panel produced a
    **stable refuting edge** bearing on it — a ``REFUTES``
    :class:`~iknos.core.edge_judge.EdgeJudgment` with ``sign_stable=True``. The mapping is
    conservative by construction:

    - A stable ``REFUTES`` judgment present ⇒ :attr:`~ChannelStance.AFFIRM` (the panel agreed on a
      refutation). The detail names the strongest such edge.
    - ``REFUTES`` judgment(s) present but **all sign-unstable** (the panel split direction,
      ``sign_stable=False``) ⇒ :attr:`~ChannelStance.ABSTAIN`. This is precisely the §13 finding the
      producer surfaces and "the gate must clear before a ``refuted`` flip": an unstable panel is
      *insufficient* consistency, not evidence against, so it abstains (and a required-LLM gate
      withholds) — never silently averaged into an affirmation.
    - **No** ``REFUTES`` judgment ⇒ :attr:`~ChannelStance.ABSTAIN` (the panel surfaced no refutation
      to affirm). The LLM channel does not *dissent* merely from absence — dissent is reserved for a
      positive contra-signal a later refinement may add (e.g. a strong stable ``SUPPORTS`` plurality
      against a structurally-refuted hypothesis); absence is abstention.

    ``judgments`` may be a whole hypothesis's edge set (mixed signs) or pre-filtered; passing
    ``hypothesis`` restricts to edges bearing on that hypothesis (a no-op when the caller already
    filtered). Pure — the producer/consumer wires it to the real
    :class:`~iknos.core.edge_judge.HypothesisJudgment`.
    """
    refuters = [
        j
        for j in judgments
        if j.sign is EdgeSign.REFUTES and (hypothesis is None or j.hypothesis == hypothesis)
    ]
    if not refuters:
        return abstaining(GateChannel.LLM, "panel surfaced no refuting edge")

    stable = [j for j in refuters if j.sign_stable]
    if not stable:
        # Every refuter split direction — the §13 finding to clear, not an affirmation.
        return abstaining(
            GateChannel.LLM, f"all {len(refuters)} refuting edge(s) sign-unstable (panel split)"
        )

    strongest = max(stable, key=lambda j: j.strength)
    return affirming(
        GateChannel.LLM,
        f"{len(stable)} stable refuting edge(s); strongest {strongest.evidence}→"
        f"{strongest.hypothesis} strength={strongest.strength:.3f}",
    )


#: Default ``detail`` for an unbuilt ``SYMBOLIC`` / not-yet-wired ``TEMPORAL`` abstention.
_SYMBOLIC_UNWIRED = "symbolic sub-region not built — use core/symbolic_gate.symbolic_channel_for"
_TEMPORAL_UNWIRED = "temporal check not applicable / not yet wired (G4.5)"


def symbolic_channel(detail: str = _SYMBOLIC_UNWIRED) -> ChannelSignal:
    """The **SYMBOLIC** channel's ABSTAIN seam — the default when no sub-region was built (§8).

    The real producer landed in W3: :func:`iknos.core.symbolic_gate.symbolic_channel_for` runs a
    clingo consistency check over the affected sub-region and returns an :func:`affirming` /
    :func:`dissenting` / :func:`abstaining` signal. This function remains the **safe default** a
    caller passes when it has *not* assembled that sub-region: a present-but-abstaining seam, so
    :data:`DEFAULT_GATE` — which *requires* this channel — withholds (the structural refutation is
    surfaced, never auto-persisted, while the symbolic check has nothing to say). §7.2, principle 6.
    """
    return abstaining(GateChannel.SYMBOLIC, detail)


def temporal_channel(detail: str = _TEMPORAL_UNWIRED) -> ChannelSignal:
    """The **TEMPORAL** channel — ABSTAINs until its bitemporal-validity producer lands (§7.4).

    Conditionally applicable ("where time matters", §6): under :data:`DEFAULT_GATE` it is *not*
    required, so abstaining here does not by itself block a flip — but when the producer (a later
    slice) finds the timeline *forbids* the contradiction it returns a :func:`dissenting` signal,
    which vetoes under every policy. ABSTAIN is the honest default both when time is irrelevant and
    before the producer exists.
    """
    return abstaining(GateChannel.TEMPORAL, detail)


def authorise_from_panel(
    judgments: Iterable[EdgeJudgment],
    *,
    hypothesis: NodeId | None = None,
    symbolic: ChannelSignal | None = None,
    temporal: ChannelSignal | None = None,
    gate: RefutationGate = DEFAULT_GATE,
) -> GateDecision:
    """Convenience: assemble the three channels and :func:`authorise` in one call.

    Derives the LLM channel from the panel (:func:`llm_channel`) and takes the symbolic/temporal
    channels as-given (defaulting to their ABSTAIN seams), then applies ``gate``. This is the shape
    the ``persist_verdicts`` filter / ``find-contradiction`` operator (later slices) call per
    structurally-refuted hypothesis — kept here, pure, so those consumers stay thin. Pass an
    explicit ``symbolic=``/``temporal=`` signal once its producer exists; until then the defaults
    keep the gate conservative.
    """
    signals = [
        llm_channel(judgments, hypothesis=hypothesis),
        symbolic if symbolic is not None else symbolic_channel(),
        temporal if temporal is not None else temporal_channel(),
    ]
    return authorise(signals, gate=gate)


def authorised_hypotheses(
    judged: Mapping[NodeId, Sequence[EdgeJudgment]],
    *,
    structurally_refuted: Iterable[NodeId],
    symbolic: Mapping[NodeId, ChannelSignal] | None = None,
    temporal: Mapping[NodeId, ChannelSignal] | None = None,
    gate: RefutationGate = DEFAULT_GATE,
) -> dict[NodeId, GateDecision]:
    """Run the gate over every structurally-refuted hypothesis — the consumer-facing batch form.

    For each hypothesis the QBAF flagged ``refuted`` (``structurally_refuted``), assemble its
    channels (its panel edges from ``judged``; its symbolic/temporal signals from the optional maps,
    defaulting to the ABSTAIN seams) and :func:`authorise`. Returns a decision per hypothesis, in
    sorted id order (§10 replay). The caller persists a flip only where ``decision.authorised`` and
    surfaces the rest as findings (``decision.is_finding``) — the gate decides, the caller acts.

    Pure: the ``persist_verdicts`` filter (a later slice) maps the QBAF verdicts + the produced
    judgments through this, then writes only the authorised flips.
    """
    sym = symbolic or {}
    tmp = temporal or {}
    decisions: dict[NodeId, GateDecision] = {}
    for hid in sorted(set(structurally_refuted)):
        decisions[hid] = authorise_from_panel(
            judged.get(hid, ()),
            hypothesis=hid,
            symbolic=sym.get(hid),
            temporal=tmp.get(hid),
            gate=gate,
        )
    return decisions
