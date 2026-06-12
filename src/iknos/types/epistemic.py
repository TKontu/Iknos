"""Structured epistemic-field vocabulary for the proposition layer (§3.1, §10).

A proposition is not just a string: §3.1 requires its epistemic operators be kept as
**structured fields, never flattened into text** — a *negated* / *probable* /
*reported* claim is materially different from a bare assertion, and downstream
reasoning (sign, credibility weighting, the entailment verifier) reads these fields.
This module is the property contract for those fields on the ``Proposition`` AGE
label, plus the fact-vs-judgement routing derived from ``epistemic_class`` (§3.1/§5).

Two things deliberately live here and **not** as model-emitted fields:

- ``faithfulness`` (does the proposition represent its span?) is **not** in this
  module's emit path. §3.1 is explicit: *"Confidence comes from consistency and
  verification, not verbalized self-report"* and *"LLM attention weights are not a
  faithfulness signal."* It is the calibrated output of multi-sample extraction
  (G1.3) + the extract-then-verify NLI pass (G1.4), so it is owned by those
  increments — null until then.
- ``provisional`` is a **system** gate, not a model judgement: §10 sets it "when
  faithfulness or a binding is below the stakes-dependent threshold". It is carried as a
  **set of reasons** (:class:`ProvisionalReason`), not one boolean (R8) — triage (§11.1)
  needs *why*, the quarantine gate (R9) needs only non-emptiness. :func:`provisional_reasons_for`
  derives the faithfulness leg; other producers (the propositionizer's polarity twins, the
  reference binder) OR-fold their own reasons in. A legacy boolean is written alongside for
  one transition release (:func:`legacy_provisional`).

All enums are ``StrEnum`` so they serialize to plain strings for the AGE layer
(``db/age.py:cypher_map``), exactly like ``Tier`` / ``SensitivityLevel``. (For
``Routing`` this is not stylistic: a plain ``Enum`` would fall through ``cypher_map``
to ``json.dumps`` and persist as ``'Routing.FACT'``.)
"""

import json
from collections.abc import Callable, Collection, Iterable
from enum import StrEnum
from typing import Any


class Polarity(StrEnum):
    """Whether the proposition's content is asserted or negated (§3.1, §10).

    Carries the *sign* of the propositional content: ``text`` holds the affirmative
    content and ``polarity`` says whether it is asserted or denied, so a negated
    claim ("the bearing did not fail") is stored as ``text="The bearing failed."`` +
    ``NEGATED`` — never as surface double-negation. This is what lets a negated claim
    support the opposite hypothesis (§3.1) and keeps the G1.4 entailment check stable.
    """

    ASSERTED = "asserted"
    NEGATED = "negated"


class Modality(StrEnum):
    """The claim's epistemic modality (§3.1, §10) — orthogonal to ``EpistemicClass``."""

    CATEGORICAL = "categorical"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    HYPOTHESIZED = "hypothesized"


class Attribution(StrEnum):
    """Who asserts the claim (§3.1, §10): the document itself, reported speech, or a
    named source's claim. Feeds conditional credibility (§9.1)."""

    DOCUMENT = "document"
    REPORTED_SPEECH = "reported-speech"
    NAMED_SOURCE = "named-source"


class EpistemicClass(StrEnum):
    """Observation vs testimony vs judgement (§3.1, §10) — orthogonal to modality.

    Gates how much source credibility applies (§9.1) and drives routing (:func:`route_for`):
    an **observation** ("the rolling surface shows particle indentations") stands
    largely source-independently and ingests as a *fact*; **testimony**/**judgement**
    ("therefore it was an assembly fault") are credibility-weighted and ingest as
    defeasible *judgement-claims* the engine re-derives — never as facts.
    """

    OBSERVATION = "observation"
    TESTIMONY = "testimony"
    JUDGEMENT = "judgement"


class Entailment(StrEnum):
    """The verifier's NLI verdict: does the source span entail the proposition? (§3.1, G1.4).

    Judged against the span alone (the source of truth), not world knowledge — so it
    catches both hallucinated content (``NEUTRAL`` — not in the source) and a claim the
    source actively denies (``CONTRADICTED`` — the span says the opposite). Categorical,
    never a number: faithfulness is *derived* from this verdict (:func:`faithfulness_from_verdict`),
    never self-reported (§3.1: "confidence comes from consistency and verification").
    """

    ENTAILED = "entailed"
    NEUTRAL = "neutral"
    CONTRADICTED = "contradicted"


class Routing(StrEnum):
    """How a proposition ingests into the graph (§3.1/§5, G1.2).

    A **cached derivation** of ``epistemic_class`` (see :func:`route_for`) — persisted
    so the Phase-2 graph layer reads it directly, but the invariant
    ``routing == route_for(epistemic_class)`` must always hold; never set independently.
    """

    FACT = "fact"
    JUDGEMENT = "judgement"


# Single source of truth for the routing rule (§3.1/§5). Keyed on **every**
# EpistemicClass member so adding one raises a KeyError (fail-loud on vocabulary
# growth) rather than silently defaulting — cf. the _SENSITIVITY_RANK exhaustiveness
# convention in governance.py.
_ROUTING: dict[EpistemicClass, Routing] = {
    EpistemicClass.OBSERVATION: Routing.FACT,
    EpistemicClass.TESTIMONY: Routing.JUDGEMENT,
    EpistemicClass.JUDGEMENT: Routing.JUDGEMENT,
}


def route_for(epistemic_class: EpistemicClass) -> Routing:
    """Fact-vs-judgement routing for a proposition's epistemic class (§3.1/§5, G1.2)."""
    return _ROUTING[epistemic_class]


# Placeholder, stakes-dependent calibration is G1.6. Single source of truth for the
# provisional gate — the *only* place the threshold is encoded.
_FAITHFULNESS_PROVISIONAL_THRESHOLD: float = 0.5


class ProvisionalReason(StrEnum):
    """Why a proposition is quarantined from high-stakes moves (§3.1, §11.1).

    A proposition carries a **set** of these, not one boolean (R8): §11.1 triage needs to
    know *why* an atom is provisional (the stakes-dependent threshold differs by reason), and
    the R9 quarantine gate needs only whether the set is non-empty. Producers across phases
    each contribute their own reason and OR-fold into the set, never clearing another's:

    - ``LOW_FAITHFULNESS`` — faithfulness below the gate threshold (Phase 1, G1.5); derived
      by :func:`provisional_reasons_for`.
    - ``UNASSESSED_FAITHFULNESS`` — no faithfulness was computed at all (the verifier-off /
      verifier-unavailable degraded mode, Phase 1, G1.21); also derived by
      :func:`provisional_reasons_for` (from a ``None`` faithfulness). §3.1 D2: unassessed
      grounding is provisional, *never coerced toward trusted* — so a degraded-mode atom is
      quarantined until a verifier later completes its faithfulness (G1.22 backfill).
    - ``POLARITY_UNSTABLE`` — multi-sample extraction wavered on the claim's sign, so both
      polarity twins are quarantined (Phase 1, G1.14); set by the propositionizer
      independently of faithfulness, so it survives the verifier-off / degraded mode.
    - ``UNRESOLVED_REFERENCE`` — a mention the proposition rests on stayed unresolved or only
      candidate-bound (Phase 2, G2.4); set by the reference binder.
    - ``UNINFERRED_BUDGET`` — re-inference was deferred by the VoI budget (Phase 5); no
      producer yet, reserved here so the vocabulary is complete.
    """

    LOW_FAITHFULNESS = "low_faithfulness"
    UNASSESSED_FAITHFULNESS = "unassessed_faithfulness"
    POLARITY_UNSTABLE = "polarity_unstable"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    UNINFERRED_BUDGET = "uninferred_budget"


def provisional_reasons_for(
    faithfulness: float | None, *, threshold: float = _FAITHFULNESS_PROVISIONAL_THRESHOLD
) -> set[ProvisionalReason]:
    """The **faithfulness-derived** provisional reasons for a proposition (§3.1, §10).

    ``{LOW_FAITHFULNESS}`` when faithfulness is below the threshold, ``{UNASSESSED_FAITHFULNESS}``
    when faithfulness is ``None``, else the empty set. ``None`` is the verifier-off /
    verifier-unavailable degraded mode: no faithfulness was computed, and §3.1 D2 (G1.21) decides
    that unassessed grounding is **provisional, never coerced toward trusted** — so this axis
    contributes a reason rather than abstaining (other producers may still OR-fold their own —
    see :class:`ProvisionalReason`). This *changes* the pre-G1.21 verifier-off behavior (``None``
    → empty); the degraded-mode tests were repinned deliberately. Boundary is half-open
    (``< threshold`` → provisional), mirroring :func:`intentional.band`. Raises for an
    out-of-range faithfulness — defined only on ``[0, 1]``, so an out-of-range value is a caller
    bug, surfaced not clamped.

    Replaces the former ``is_provisional`` boolean gate (R8): one flag carried three meanings;
    the reason set lets triage (§11.1) act on *why* and the quarantine gate (R9) on whether
    any reason is present.
    """
    if faithfulness is None:
        return {ProvisionalReason.UNASSESSED_FAITHFULNESS}
    if not 0.0 <= faithfulness <= 1.0:
        raise ValueError(f"faithfulness must be in [0, 1], got {faithfulness!r}")
    return {ProvisionalReason.LOW_FAITHFULNESS} if faithfulness < threshold else set()


def merge_provisional_reasons(*groups: Iterable[str | ProvisionalReason]) -> list[str]:
    """OR-fold reason groups into a stable, deduped, sorted ``list[str]`` (§3.1).

    Provisional reasons only ever accumulate — a binding that stays open cannot clear a
    low-faithfulness flag (the "never cleared" discipline). Sorted + deduped so the persisted
    list is order-stable regardless of which producer ran first.
    """
    merged: set[str] = set()
    for group in groups:
        merged.update(str(r) for r in group)
    return sorted(merged)


# The faithfulness-axis reasons (:func:`provisional_reasons_for`). Mutually exclusive — a
# proposition is at most one of "assessed-and-low" / "unassessed" — and **re-derived** whenever
# faithfulness is (re)assessed: a verify-backfill (G1.22) that completes a previously-unassessed
# faithfulness replaces ``UNASSESSED_FAITHFULNESS`` with the now-assessed reason. This is *not* a
# violation of the "never cleared" discipline: that discipline forbids one producer clearing
# *another* axis's reason; the faithfulness axis legitimately owns and re-derives its own.
_FAITHFULNESS_AXIS_REASONS: frozenset[ProvisionalReason] = frozenset(
    {ProvisionalReason.LOW_FAITHFULNESS, ProvisionalReason.UNASSESSED_FAITHFULNESS}
)


def reassess_faithfulness_reasons(
    existing: Iterable[str | ProvisionalReason],
    faithfulness: float | None,
    *,
    threshold: float = _FAITHFULNESS_PROVISIONAL_THRESHOLD,
) -> list[str]:
    """Recompute a proposition's reason set after its faithfulness is (re)assessed (R8/G1.22).

    The faithfulness-axis reasons (:data:`_FAITHFULNESS_AXIS_REASONS`) are dropped from ``existing``
    and re-derived from the new ``faithfulness`` via :func:`provisional_reasons_for`; **non**-axis
    reasons (a G1.14 ``POLARITY_UNSTABLE`` twin, a Phase-2 ``UNRESOLVED_REFERENCE``) are preserved
    untouched. This is the verify-backfill update: a span extracted with no verifier carries
    ``UNASSESSED_FAITHFULNESS``; once the verifier later runs, that reason is *replaced* (not merely
    OR-folded) by the assessed result — ``UNASSESSED`` is the one reason that must clear once
    grounding is actually assessed. Contrast the add-only fold
    (``core/proposition._with_faithfulness_reason``) used when no faithfulness-axis reason is set.
    """
    axis = {str(r) for r in _FAITHFULNESS_AXIS_REASONS}
    preserved = (str(r) for r in existing if str(r) not in axis)
    return merge_provisional_reasons(
        preserved, provisional_reasons_for(faithfulness, threshold=threshold)
    )


def decode_provisional_reasons(value: Any) -> list[str]:
    """Decode a persisted ``provisional_reasons`` property into a ``list[str]``.

    Stored via ``cypher_map`` as a JSON-string property, so on read it comes back as a JSON
    string; a pure round-trip may hand back a real list, and a missing property is ``None``.
    Accept all three (mirrors ``boxes/serde._as_str_list``).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(x) for x in json.loads(str(value))]


def legacy_provisional(faithfulness: float | None, reasons: Collection[str]) -> bool | None:
    """The legacy ``provisional`` boolean for the one-release transition window (R8).

    Reproduces the pre-R8 tri-state exactly so readers of the boolean stay correct until they
    migrate to ``provisional_reasons``: ``None`` when nothing has been determined (no
    faithfulness computed *and* no other reason — e.g. single-pass with no verifier), else
    ``True`` iff any reason is present. **Remove together with every read of the boolean** once
    the transition release ships (see the R8 removal TODOs at the call sites).
    """
    if faithfulness is None and not reasons:
        return None
    return bool(reasons)


# Single source of truth for the verify-derived faithfulness score (§3.1, G1.4/G1.5).
# Keyed on **every** Entailment member so adding one raises a KeyError (fail-loud on
# vocabulary growth) rather than silently defaulting — cf. _ROUTING above. The base is
# the *content-support* axis: CONTRADICTED is worst (the span asserts the opposite — an
# actively-wrong atom), NEUTRAL is unsupported/hallucinated (below the provisional
# threshold by design), ENTAILED earns full marks before operator penalties.
_ENTAILMENT_BASE: dict[Entailment, float] = {
    Entailment.CONTRADICTED: 0.0,
    Entailment.NEUTRAL: 0.30,
    Entailment.ENTAILED: 1.00,
}

# The *operator-preservation* axis: even an entailed proposition is corrupted if the
# verifier finds an operator was dropped. Multiplicative penalties (not additive) so a
# non-entailed verdict cannot be rescued by preserved operators. A dropped negation
# inverts the truth value (a sign flip — severe); a flattened hedge only over-states
# certainty (moderate). Tuned against _FAITHFULNESS_PROVISIONAL_THRESHOLD so a dropped
# negation falls below it (quarantined) while a flattened hedge stays above it.
_POLARITY_DROP_FACTOR: float = 0.40
_MODALITY_FLATTEN_FACTOR: float = 0.70


def faithfulness_from_verdict(
    entailment: Entailment, polarity_preserved: bool, modality_preserved: bool
) -> float:
    """Derive faithfulness ∈ [0, 1] from a verifier verdict (§3.1, G1.5).

    Faithfulness is *derived from verification*, never self-reported (§3.1). A per-verdict
    content-support base (:data:`_ENTAILMENT_BASE`) is scaled by independent multiplicative
    penalties for dropped polarity / flattened modality — silent operator corruption that an
    entailment check alone would miss. Multiplicative so a ``NEUTRAL``/``CONTRADICTED`` verdict
    cannot be rescued by preserved operators.

    **G1.3 seam:** this is the *verify component* of faithfulness. The multi-sample agreement
    signal (G1.3) combines with this value via :func:`combine_faithfulness`, so callers must
    treat this as one input, not the final word.
    """
    score = _ENTAILMENT_BASE[entailment]  # fail-loud on an unmapped verdict
    if not polarity_preserved:
        score *= _POLARITY_DROP_FACTOR
    if not modality_preserved:
        score *= _MODALITY_FLATTEN_FACTOR
    return score


def calibrate_agreement(agreement: float) -> float:
    """Calibrate raw multi-sample agreement before it folds into faithfulness (§3.1, G1.20).

    Raw cross-sample agreement is a *coarse* estimator — at N=3 it takes only the values
    ``{0, ⅓, ⅔, 1}``, and a small-N proportion over-states confidence at the extremes — so §3.1
    calls for a mild concave / Wilson-style map that pulls agreement toward the conservative side
    before it multiplies into faithfulness. This ships as the **identity** until Trial A3 fits the
    per-model curve: the seam lands now so that ``PROP_AGREEMENT_THRESHOLD`` is calibrated (Trial
    A5) against the *final* code path rather than one that later changes shape — and while the curve
    is identity, behavior is byte-identical to pre-G1.20.

    Contract for a fitted curve: a map ``[0, 1] → [0, 1]`` that is **conservative**
    (``f(a) <= a`` — calibration may only *lower* confidence, never inflate it) and monotonic
    non-decreasing. The **raw** agreement stays the persisted value (``Proposition.agreement``);
    calibration happens only here, at combine time, so refitting the curve never requires rewriting
    stored data.
    """
    if not 0.0 <= agreement <= 1.0:
        raise ValueError(f"agreement must be in [0, 1], got {agreement!r}")
    return agreement


def combine_faithfulness(
    verify: float,
    agreement: float,
    parse_quality: float = 1.0,
    *,
    agreement_curve: Callable[[float], float] = calibrate_agreement,
) -> float:
    """Combine the **three** independent faithfulness signals into the final score ∈ [0, 1]
    (§3.1: "confidence comes from consistency *and* verification"; §1: "parse quality =
    faithfulness input").

    The three signals are independent *defects*, each a factor in [0, 1]:

    - ``verify`` — the verify component (:func:`faithfulness_from_verdict`): does the span
      support the proposition with its operators?
    - ``agreement`` — the multi-sample agreement signal (G1.3,
      :func:`~iknos.core.consistency.agreement_of`): did the extractor reliably re-produce it?
    - ``parse_quality`` — the parse-provenance penalty (G1.0,
      :func:`~iknos.core.parse.parse_quality_factor`): a proposition read off a scanned/
      handwritten region is less trustworthy *at the source*, before any verification (§1, §3.1
      "mark scanned/handwritten/complex-table parses lower-faithfulness → provisional → triage").

    **Multiplicative**, mirroring the operator penalties in :func:`faithfulness_from_verdict`:
    a defect on any axis pulls the score down and **cannot be rescued** by the others — a
    verified-but-*unstable* proposition (agreement ≈ 0.33) or a verified-but-*badly-parsed* one
    (a handwritten source) is pulled below the provisional threshold even though the verifier
    passed it. Degenerate identities: ``agreement = 1.0`` (single-sample / N=1) and
    ``parse_quality = 1.0`` (digital / unknown parse) are both no-ops, so this reduces exactly to
    the verify component for the common clean-text path. All inputs are defined on [0, 1]; an
    out-of-range value is a caller bug, surfaced rather than silently clamped.

    **Calibration seam (G1.20):** the multi-sample ``agreement`` is mapped through
    ``agreement_curve`` (default :func:`calibrate_agreement`, identity) *before* it multiplies in,
    so Trial A3 can swap a per-model concave / Wilson curve here without touching this contract or
    the persisted raw agreement — and ``PROP_AGREEMENT_THRESHOLD`` (Trial A5) is fit against this
    final path. A fitted curve is conservative (``f(a) <= a``), so a non-identity curve can only
    move faithfulness *down*. The parse-quality factor's per-quality penalty is its own calibration
    target (:func:`~iknos.core.parse.parse_quality_factor`). The raw product remains the
    pre-calibration default while the curve is identity.
    """
    if not 0.0 <= verify <= 1.0:
        raise ValueError(f"verify must be in [0, 1], got {verify!r}")
    if not 0.0 <= agreement <= 1.0:
        raise ValueError(f"agreement must be in [0, 1], got {agreement!r}")
    if not 0.0 <= parse_quality <= 1.0:
        raise ValueError(f"parse_quality must be in [0, 1], got {parse_quality!r}")
    calibrated = agreement_curve(agreement)
    if not 0.0 <= calibrated <= 1.0:
        raise ValueError(
            f"agreement_curve must map into [0, 1], got {calibrated!r} for agreement {agreement!r}"
        )
    return verify * calibrated * parse_quality
