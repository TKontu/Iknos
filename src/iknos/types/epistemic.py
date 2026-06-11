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
  faithfulness or a binding is below the stakes-dependent threshold". Both inputs
  are absent in this increment, so :func:`is_provisional` is landed here (single
  tunable threshold) but **not yet called** — G1.4/G1.5/G1.6 plug into it.

All enums are ``StrEnum`` so they serialize to plain strings for the AGE layer
(``db/age.py:cypher_map``), exactly like ``Tier`` / ``SensitivityLevel``. (For
``Routing`` this is not stylistic: a plain ``Enum`` would fall through ``cypher_map``
to ``json.dumps`` and persist as ``'Routing.FACT'``.)
"""

from enum import StrEnum


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


def is_provisional(
    faithfulness: float, *, threshold: float = _FAITHFULNESS_PROVISIONAL_THRESHOLD
) -> bool:
    """Whether a proposition is provisional given its faithfulness (§3.1, §10).

    A proposition below the threshold is quarantined from high-stakes moves (a
    ``REFUTES`` that overturns a hypothesis) until confirmed. Boundary is half-open
    (``< threshold`` → provisional), mirroring :func:`intentional.band`. Raises for an
    out-of-range value — faithfulness is defined only on ``[0, 1]``, so an out-of-range
    value is a caller bug, surfaced rather than silently clamped.

    **Not called in this increment (G1.1):** faithfulness (G1.4/G1.5) and binding
    confidence (G1.7) do not exist yet, so there is nothing to gate on — the threshold
    is landed here for those increments to call; until then ``provisional`` is null.
    """
    if not 0.0 <= faithfulness <= 1.0:
        raise ValueError(f"faithfulness must be in [0, 1], got {faithfulness!r}")
    return faithfulness < threshold


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


def combine_faithfulness(verify: float, agreement: float, parse_quality: float = 1.0) -> float:
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

    **Calibration seam:** the raw product is the pre-calibration default. Trial A3 fits a
    per-model consistency-vs-correctness map (and the parse-quality factor's per-quality penalty
    is its own calibration target, :func:`~iknos.core.parse.parse_quality_factor`) that swaps in
    here without a contract change.
    """
    if not 0.0 <= verify <= 1.0:
        raise ValueError(f"verify must be in [0, 1], got {verify!r}")
    if not 0.0 <= agreement <= 1.0:
        raise ValueError(f"agreement must be in [0, 1], got {agreement!r}")
    if not 0.0 <= parse_quality <= 1.0:
        raise ValueError(f"parse_quality must be in [0, 1], got {parse_quality!r}")
    return verify * agreement * parse_quality
