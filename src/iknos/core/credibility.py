"""Conditional source credibility (Phase 2, G2.6; architecture.md §9, §9.1, §10).

**Credibility is conditional, never a flat stored scalar — and it is gated by epistemic
class** (§9.1). For an **observation/measurement** credibility is a minor factor: the claim
stands on its merits and its risk is checked by corroboration/verification, not by
interest-discounting. For a **judgement/testimony** it is central: a source's effective
credibility on a judgement = its base reliability (the box ``reliability_prior``) × a
modifier from the alignment between the claim and the source's interest — self-serving
discounted, against-interest boosted.

So this module never *stores* a credibility number; it is the canonical **computation** over
the stored *inputs* (§10 ``credibility``): ``reliability_prior`` + ``source_interest`` on the
``Box``, ``epistemic_class`` on the ``Proposition``, and the derived per-claim
``interest_alignment`` on the ``Fact``. Effective credibility is computed at use-time
(Phase 4's adjudication, the §8↔credibility seam) — optionally materialized for query perf,
recomputed on input change, exactly like abstraction level (§14). Keeping it derived is the
explicit anti-entanglement move: a stored scalar would collapse the conditional nature §9.1
forbids and silently fix it against later belief revision (track-record, §9.1).

Pure/DB split (the ``core/epistemic.py`` discipline for the math, the ``core/resolve.py``
discipline for the DB read): :func:`interest_modifier` / :func:`effective_credibility` are
DB-free and unit-testable; :func:`effective_credibility_of` reads the stored inputs from the
graph and applies them, with ``iknos.db.age`` imported lazily.

Deliberately **not** here (documented seams):

- **The per-claim interest-alignment judging pass.** Assigning a Fact its
  ``InterestAlignment`` (LLM/expert-flagged against the pack's source-interest patterns,
  §9.1) is a later increment; until it runs a Fact's alignment is ``None`` → ``UNKNOWN`` (the
  identity modifier), so credibility reduces to the box reliability — never a penalty on
  absence.
- **Track-record belief revision** (§9.1): lowering a source's credibility after it is caught
  in a refuted claim is a Phase-3/4 belief-revision concern; this module computes the
  point-in-time credibility from the current inputs.
- **Independence-aware corroboration** and the **coherence/triage** defenses (§9.1) compose
  *around* credibility in Phase 4, not inside this scalar.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.types.epistemic import EpistemicClass
from iknos.types.governance import InterestAlignment

# Note: iknos.db.age is imported lazily inside effective_credibility_of (see module
# docstring), so importing this module stays DB-free for the unit tests of the math.

# How much source credibility an epistemic class is *gated by* (§9.1): 0 = interest does not
# move credibility at all (an observation stands largely source-independently — its risk is
# corroboration/verification, not interest-discount), 1 = fully interest-weighted. Keyed on
# **every** EpistemicClass member so adding one fails loud (KeyError) rather than silently
# defaulting — the _ROUTING / _SENSITIVITY_RANK exhaustiveness convention.
_CLASS_INTEREST_GATE: dict[EpistemicClass, float] = {
    EpistemicClass.OBSERVATION: 0.0,  # credibility minor — interest does not discount/boost
    EpistemicClass.TESTIMONY: 1.0,  # reported claim — interest-weighted
    EpistemicClass.JUDGEMENT: 1.0,  # interpretation — fully interest-weighted
}

# The interest modifier endpoints (§9.1), applied only to the gated (judgement/testimony)
# fraction: self-serving DISCOUNTS (< 1), against-interest BOOSTS (> 1), neutral/unknown is
# the identity. Conservative by design — credibility is the *first* of four composing defenses
# (independence, track-record, coherence/triage, §9.1), not a sole gate, so the discount/boost
# are moderate rather than extreme. Keyed on every InterestAlignment member (fail-loud).
_ALIGNMENT_MODIFIER: dict[InterestAlignment, float] = {
    InterestAlignment.SELF_SERVING: 0.5,
    InterestAlignment.NEUTRAL: 1.0,
    InterestAlignment.AGAINST_INTEREST: 1.3,
    InterestAlignment.UNKNOWN: 1.0,  # no alignment judged yet → identity (defer, not penalize)
}


def interest_modifier(epistemic_class: EpistemicClass, alignment: InterestAlignment) -> float:
    """The §9.1 interest modifier on credibility, **gated by epistemic class**.

    Interpolates from ``1.0`` (no interest effect) toward the alignment endpoint by the class
    gate: an **observation** (gate 0) returns ``1.0`` for *any* alignment — its credibility is
    interest-independent — while a **judgement** (gate 1) returns the full
    self-serving-discount / against-interest-boost. The gate makes "credibility applies where
    it matters" a property of the formula, not a caller's branch.
    """
    gate = _CLASS_INTEREST_GATE[epistemic_class]  # fail-loud on an unmapped class
    raw = _ALIGNMENT_MODIFIER[alignment]  # fail-loud on an unmapped alignment
    return 1.0 + gate * (raw - 1.0)


def effective_credibility(
    reliability_prior: float,
    epistemic_class: EpistemicClass,
    alignment: InterestAlignment = InterestAlignment.UNKNOWN,
) -> float:
    """Effective per-claim credibility ∈ [0, 1] (§9.1) — **derived, never stored**.

    ``reliability_prior`` (the box's base reliability) × :func:`interest_modifier`, clamped to
    ``[0, 1]``. The against-interest boost can drive a high-reliability source to the ``1.0``
    ceiling (an admission against interest is maximally credible); the clamp absorbs it. The
    default ``alignment=UNKNOWN`` means "no alignment pass has judged this claim" → the box
    reliability passes through unmodified (defer, never penalize on absence). Distinct from
    ``faithfulness`` (§3.1) and edge ``strength`` (§8) — three separate quantities (§3.1/§8).

    Raises for an out-of-range ``reliability_prior`` — it is defined only on ``[0, 1]``, so an
    out-of-range value is a caller bug surfaced rather than silently clamped (the
    ``epistemic.combine_faithfulness`` convention).
    """
    if not 0.0 <= reliability_prior <= 1.0:
        raise ValueError(f"reliability_prior must be in [0, 1], got {reliability_prior!r}")
    raw = reliability_prior * interest_modifier(epistemic_class, alignment)
    return max(0.0, min(1.0, raw))


async def effective_credibility_of(session: AsyncSession, fact_id: uuid.UUID) -> float | None:
    """Compute a Fact's effective credibility from its stored inputs (§9.1) — the use-time read.

    Walks the stored inputs Phase 2 seeded: the Fact's box ``reliability_prior`` (Fact →
    ``Box`` by id), the Fact's ``interest_alignment`` slot, and the epistemic class of the
    Proposition it is ``EVIDENCED_BY``. Returns ``None`` when an input is missing (no box
    reliability, or no evidencing proposition) — credibility is undefined, not zero, when the
    chain is incomplete. This is the canonical read Phase 4 adjudication consumes; it is here
    (not duplicated at the call site) so the derived-not-stored contract has one implementation.
    """

    from iknos.db.age import execute_cypher, unquote_agtype

    fid = str(fact_id)

    rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {{id: '{fid}'}}) "
        "OPTIONAL MATCH (b:Box {id: f.box}) "
        "OPTIONAL MATCH (f)-[:EVIDENCED_BY]->(p:Proposition) "
        "RETURN b.reliability_prior, p.epistemic_class, f.interest_alignment",
        returns="rel agtype, eclass agtype, align agtype",
    )
    if not rows:
        return None
    rel_raw, eclass_raw, align_raw = rows[0]
    if rel_raw is None or str(rel_raw) == "null":
        return None
    if eclass_raw is None or str(eclass_raw) == "null":
        return None

    reliability = float(str(rel_raw))
    epistemic_class = EpistemicClass(unquote_agtype(eclass_raw))
    alignment = (
        InterestAlignment(unquote_agtype(align_raw))
        if align_raw is not None and str(align_raw) != "null"
        else InterestAlignment.UNKNOWN
    )
    return effective_credibility(reliability, epistemic_class, alignment)
