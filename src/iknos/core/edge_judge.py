"""Blind, randomized, multi-sample edge judge (Phase 4, G4.3 slice 2; architecture §8).

The LLM-bound layer between the G4.2 candidate funnel and the G4.3-slice-1 subjective-logic
read-off. Candidate generation (``core/candidates.py``) decides *which* ``(evidence → hypothesis)``
pairs are worth assessing; the subjective-logic core (``core/subjective_logic.py``) is the *pure
algebra* that turns per-sample agreement counts into a calibrated ``strength``. This module is the
piece in between: the **elicitation** that produces those counts under the §8 bias disciplines.
It is to ``subjective_logic.py`` what ``verify.py`` is to ``epistemic.faithfulness_from_verdict``
— the LLM judge that *generates* the categorical evidence the pure scorer consumes. No DB, no AGE,
no migration: it reuses :class:`~iknos.core.llm.LLMClient` verbatim and judges plain text, so it
runs in a concurrent phase and is unit-testable with a mock LLM. The **AGE producer** (slice 3,
deferred) reads the candidate pool out of the graph, calls this judge, and writes the surviving
``SUPPORTS``/``REFUTES`` edges carrying the strength — the Phase-4 analogue of how the
propositionizer wraps the ``Verifier``.

**The four §8 disciplines this layer implements (the bias-hardened judgment):**

1. **Sign before magnitude.** The model classifies *direction only* — ``supports`` / ``refutes`` /
   ``irrelevant`` — and is **never asked for a number**. §8: "do not feed raw verbalized LLM
   confidence as edge weight." Magnitude is not elicited; it *emerges* from cross-sample
   consistency (discipline 4). An ``irrelevant`` plurality drops the pair (precision late, §5.1) —
   strength is estimated only for the non-irrelevant survivors.
2. **Blind.** The prompt carries the hypothesis statement and the evidence text and **nothing about
   the hypothesis's current acceptability/state** — the sycophancy guard (§8). The judge cannot
   anchor on "the system already believes this", because it is never told.
3. **Randomized (relative, not absolute).** All of a hypothesis's candidate evidence is presented
   *together* (so the judge weighs items relative to each other, §8 "relative, not absolute"), and
   the **presentation order is permuted per sample** — the position-bias guard (§8). The permutation
   is a deterministic, content-addressed function of ``(hypothesis_id, sample_index)`` (see
   :func:`_permutation`), so a run is **replayable** (§10) yet each sample sees a different order.
   This is also why multi-sampling yields signal **even at temperature 0**: distinct orders are
   distinct prompts, so a position-biased model disagrees with itself across samples — the
   disagreement *is* the consistency measurement.
4. **Multi-sample consistency → opinion.** Of the ``N`` samples, the per-item votes are tallied
   and mapped to the ``(positive, negative)`` counts
   :func:`~iknos.core.subjective_logic.opinion_from_evidence` consumes (``positive`` = votes for
   the dominant direction, ``negative`` = votes for the opposite
   direction, ``irrelevant`` votes **abstain** — neither, so they *raise* the opinion's uncertainty
   exactly as the SL docstring says). The resulting opinion is **discounted by source reliability**
   (§8 ↔ §9.1) and its projected probability is the calibrated edge ``strength`` ∈ [0, 1] the QBAF
   consumes (§10). Sign instability (both directions voted) is **surfaced as a finding**
   (``sign_stable=False``, §13), never smoothed — the analogue of the G1.14 polarity twins, and the
   signal the ensemble gate (§7.2, G4.5) consumes before it will authorise a persisted ``refuted``
   flip.

**Decisions recorded eyes-open (so they are not re-litigated):**

- **Diversity from order-randomization, not (only) temperature.** Unlike the multi-sample extractor
  (``proposition.py``, which *requires* ``temperature>0`` because identical prompts would give
  identical samples), this judge gets its sample diversity from the per-sample permutation, so the
  default ``temperature=0`` is deliberate — deterministic, replayable judging. The one case the
  permutation cannot diversify is a hypothesis with a *single* candidate (no order to vary): there,
  ``N`` greedy samples agree trivially and the raw consistency is optimistic. That optimism is
  absorbed at the **per-model recalibration seam** (step 2, ``opinion_from_evidence``'s prior
  weight / a fitted curve, identity until G4.6), not papered over here.
- **Irrelevant-plurality drop (recall → precision handoff).** A pair is dropped iff ``irrelevant``
  is the **strict plurality** of the panel (more irrelevant votes than either direction). Anything
  with a directional plurality survives, its abstentions kept as uncertainty rather than discarded —
  recall-leaning at the margin, with the §8 LLM stage doing the bulk of the precision the funnel
  (recall-first, G4.2) left to it. The threshold is one explicit rule, tunable, not scattered.
- **No fusion here.** A single model's ``N`` samples fold into **one** opinion via the consistency
  counts (that *is* the multi-sample fold); :func:`~iknos.core.subjective_logic.fuse` combines the
  opinions of *independent judges* (varied models + the symbolic/temporal channels) and is the
  ensemble gate's job (G4.5). The full :class:`~iknos.core.subjective_logic.Opinion` is exposed on
  each :class:`EdgeJudgment` so that stage can fuse without re-eliciting.

**Sign is structural downstream.** ``supports``/``refutes`` here *becomes* the ``SUPPORTS`` vs
``REFUTES`` edge **type** the producer writes (§10) — the categorical sign decided first and
separately, exactly as ``qbaf_adapter`` reads it back off the edge label. The judge owns the
classification; the graph owns its persistence.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from iknos.core.cache import canonical_json_sha256, sha256_hex
from iknos.core.llm import LLMClient
from iknos.core.prompts import vocab
from iknos.core.subjective_logic import Opinion, discount, opinion_from_evidence
from iknos.core.truth_maintenance import NodeId
from iknos.types.edges import EdgeSign

# A panel of one is not multi-sample; the §8 disciplines (consistency, position-bias guard) only
# pay off with several. Five is the default panel size — enough for a stable plurality and a
# meaningful uncertainty estimate without an N-fold cost blow-up. Tunable per call.
DEFAULT_JUDGE_SAMPLES = 5


class JudgedSign(StrEnum):
    """How one evidence item bears on a hypothesis, as the blind judge classifies it (§8).

    A **three-way** classification (the §8 "sign before magnitude" step): the two directional signs
    map onto the ``SUPPORTS``/``REFUTES`` edge type (:class:`~iknos.types.edges.EdgeSign`), and
    ``IRRELEVANT`` is the third option that lets a candidate be *rejected* at the LLM stage (the
    precision the recall-first funnel deferred, §5.1) — it never becomes an edge.
    """

    SUPPORTS = "supports"
    REFUTES = "refutes"
    IRRELEVANT = "irrelevant"

    def to_edge_sign(self) -> EdgeSign:
        """The persisted edge sign for a directional verdict; ``IRRELEVANT`` has none (raises)."""
        if self is JudgedSign.SUPPORTS:
            return EdgeSign.SUPPORTS
        if self is JudgedSign.REFUTES:
            return EdgeSign.REFUTES
        raise ValueError("IRRELEVANT has no edge sign — an irrelevant pair produces no edge")


@dataclass(frozen=True)
class JudgeEvidence:
    """One candidate evidence item to judge against a hypothesis — the DB-free judge input.

    ``id`` is the bearer reasoning node (a ``Fact``/``Conclusion``, §5), ``text`` its claim
    (the producer resolves it via ``EVIDENCED_BY`` → ``Proposition.text``). ``reliability`` is the
    source's effective credibility ∈ [0, 1] (``core/credibility.py``'s ``effective_credibility``,
    the §8 ↔ §9.1 seam): the judged opinion is **discounted** by it before the read-off, so a
    low-credibility source's judgment is pulled toward uncertainty rather than asserted at face
    value. Defaults to ``1.0`` (identity) until the producer (slice 3) wires the real credibility —
    the judge is correct with or without it, the wiring just sharpens the discount.
    """

    id: NodeId
    text: str
    reliability: float = 1.0


@dataclass(frozen=True)
class EdgeJudgment:
    """One adjudicated ``(evidence → hypothesis)`` edge — a *surviving* (non-irrelevant) candidate.

    The product the producer (slice 3) writes as a ``SUPPORTS``/``REFUTES`` edge: ``sign`` is the
    edge type, ``strength`` the calibrated weight (§10 — *never* a raw LLM number; it is the
    projected probability of the discounted, multi-sample :attr:`opinion`). ``positive`` /
    ``negative`` / ``abstained`` are the panel tally behind it (auditable on the Action, §10.1);
    ``sign_stable`` is ``False`` when the panel split *direction* (both ``supports`` and ``refutes``
    were voted) — a §13 finding the ensemble gate (G4.5) must clear before a ``refuted`` flip, never
    silently averaged away.
    """

    evidence: NodeId
    hypothesis: NodeId
    sign: EdgeSign
    strength: float
    opinion: Opinion
    positive: int
    negative: int
    abstained: int
    n_samples: int
    sign_stable: bool


@dataclass(frozen=True)
class HypothesisJudgment:
    """The judge's verdict over one hypothesis's whole candidate set.

    ``judgments`` are the survivors (each a written edge); ``irrelevant`` are the candidate evidence
    ids the panel judged *not* to bear (an ``irrelevant`` plurality) — kept so the drop is auditable
    on the Action (it was *considered and rejected*, §10.1, not silently missing). Both are
    deterministically ordered by the input evidence order so a replay is stable (§10).
    """

    hypothesis: NodeId
    judgments: tuple[EdgeJudgment, ...] = ()
    irrelevant: tuple[NodeId, ...] = ()


class _EdgeVerdict(BaseModel):
    """One per-item verdict the model emits (drives guided decoding).

    ``ref`` is the **1-based position in the presented (permuted) list** — not a node id, which the
    model never sees — so the judge maps the verdict back to the canonical item regardless of the
    sample's order. ``sign`` is the categorical direction; **no magnitude field exists** (§8: the
    model classifies sign, it does not verbalize a strength).
    """

    ref: int
    sign: JudgedSign


class EdgeVerdicts(BaseModel):
    """Structured output contract; drives vLLM guided decoding — one verdict per evidence item."""

    verdicts: list[_EdgeVerdict]


JUDGE_SCHEMA = EdgeVerdicts.model_json_schema()

# A semantic version of the judge's output contract — the manual lever for a deliberate "the
# judgment shape changed" marker, alongside the prompt_sha/schema_sha that hash the actual prompt
# and schema. Mirrors verify.py::VERIFY_SCHEMA_VERSION; the slice-3 producer folds all three into
# the edge's Action so a re-judgment under a changed pipeline is detectable.
JUDGE_SCHEMA_VERSION = 1


def _permutation(hypothesis_id: NodeId, sample_index: int, count: int) -> list[int]:
    """A deterministic per-sample permutation of ``range(count)`` — the §8 position-bias guard.

    Content-addressed on ``(hypothesis_id, sample_index)`` via SHA-256, **not** ``random.shuffle``
    with a process-salted seed: the order must be replayable across runs (§10 — a judgment is an
    auditable Action), yet differ per sample so the panel actually probes position sensitivity.
    A Fisher–Yates shuffle driven by successive 4-byte windows of the digest (re-hashing if the
    list is longer than the digest provides) gives a uniform, stable permutation with no RNG-seed
    portability assumptions. ``sample_index`` varies it across the panel; ``hypothesis_id`` varies
    it across hypotheses so two hypotheses' sample 0 are not the same order.
    """
    order = list(range(count))
    if count < 2:
        return order
    # A pool of deterministic bytes; extend by re-hashing with a counter if a long list needs more.
    digest = bytes.fromhex(sha256_hex(f"{hypothesis_id}|{sample_index}"))
    pool = bytearray(digest)
    cursor = 0
    counter = 0
    # Fisher–Yates from the high index down; each step consumes 4 bytes as an unbiased-enough index.
    for i in range(count - 1, 0, -1):
        if cursor + 4 > len(pool):
            counter += 1
            pool.extend(bytes.fromhex(sha256_hex(f"{hypothesis_id}|{sample_index}|{counter}")))
        word = int.from_bytes(pool[cursor : cursor + 4], "big")
        cursor += 4
        j = word % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


class EdgeJudge:
    """The blind, randomized, multi-sample §8 edge judge (G4.3 slice 2).

    Stateless across calls (the LLM client is shared); one instance judges any number of
    hypotheses. DB-free — :meth:`judge_hypothesis` takes resolved text + reliabilities and returns
    value objects, so it runs in the producer's concurrent phase (slice 3) and is testable with a
    mock client, exactly like :class:`~iknos.core.verify.Verifier`.
    """

    SCHEMA_VERSION = JUDGE_SCHEMA_VERSION

    # Vocabulary generated from the enum (never hand-typed) so the prompt's legal values cannot
    # drift from the guided-decode schema — the same single-source discipline as the extractor and
    # verifier prompts (core/prompts.py::vocab).
    SYSTEM_PROMPT = (
        "You are a blind, impartial evidence adjudicator. You are given one HYPOTHESIS and a "
        "numbered list of EVIDENCE statements that may bear on it. For EACH evidence item, "
        "classify ONLY the DIRECTION of its bearing on the hypothesis — do not score how "
        "strongly.\n"
        "Rules:\n"
        "- Judge each item on its CONTENT against the hypothesis ALONE. You are deliberately NOT "
        "told whether the hypothesis is currently believed, doubted, or undecided, and you must "
        "not assume any such prior — judge the evidence, not the standing of the claim.\n"
        "- Weigh the items relative to one another: the list is the competing evidence on this one "
        "hypothesis, presented together on purpose.\n"
        "- The numbering is arbitrary and carries no meaning — an item's position is not a hint to "
        "its importance.\n"
        "- For each item emit its `ref` (its number in the list) and a `sign` "
        f"({vocab(JudgedSign)}): "
        "`supports` = the evidence makes the hypothesis more likely; `refutes` = it makes the "
        "hypothesis less likely / contradicts it; `irrelevant` = it neither supports nor refutes "
        "the hypothesis.\n"
        "- Classify direction only. Do NOT output any number, score, probability, or confidence — "
        "the sign is the entire judgment.\n"
        "- Emit exactly one verdict per evidence item, by its `ref`.\n"
        'Return JSON of the form {"verdicts": [{"ref": 1, "sign": "supports"}, '
        '{"ref": 2, "sign": "irrelevant"}]}.'
    )

    def __init__(
        self,
        llm: LLMClient,
        *,
        n_samples: int = DEFAULT_JUDGE_SAMPLES,
        sampling: dict[str, Any] | None = None,
    ) -> None:
        if n_samples < 1:
            raise ValueError(f"n_samples must be >= 1, got {n_samples!r}")
        self.llm = llm
        self.n_samples = n_samples
        # Default greedy: the per-sample permutation (not temperature) is the diversity source, so
        # temperature 0 keeps the panel deterministic and replayable. A caller wanting independent
        # re-samples (e.g. for a single-evidence hypothesis the permutation cannot diversify) raises
        # it explicitly; nothing here forces it (cf. proposition.py, where greedy multi-sampling is
        # a misconfiguration — there the permutation guard does not exist).
        self.sampling = sampling or {"temperature": 0.0}

    def build_messages(
        self, hypothesis_text: str, presented: Sequence[JudgeEvidence]
    ) -> list[dict[str, str]]:
        """Assemble the blind chat messages for one sample, evidence in the **presented** order.

        The user message carries the hypothesis and the numbered evidence — and **no** hypothesis
        state, acceptability, or prior verdict (the blindness guard). ``presented`` is the
        per-sample permutation; the 1-based number is the ``ref`` the model echoes back.
        """
        lines = "\n".join(f"{i}. {e.text}" for i, e in enumerate(presented, start=1))
        user = f"HYPOTHESIS:\n{hypothesis_text}\n\nEVIDENCE:\n{lines}"
        return [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def prompt_sha(self) -> str:
        """SHA-256 of the judge's instruction prompt (the grading rules + interpolated vocabulary).

        The user message is pure field interpolation of the items under test — it carries no wording
        that shifts a verdict independently of the system prompt — so it is excluded, symmetric with
        the verifier's ``prompt_sha`` (G1.15). The slice-3 producer folds this into the edge's
        Action so a reworded judge is detectable without a manual version bump.
        """
        return sha256_hex(self.SYSTEM_PROMPT)

    def schema_sha(self) -> str:
        """SHA-256 of the canonical guided-decode schema (key-order-insensitive)."""
        return canonical_json_sha256(JUDGE_SCHEMA)

    async def _sample_signs(
        self,
        hypothesis_id: NodeId,
        hypothesis_text: str,
        evidence: Sequence[JudgeEvidence],
        sample_index: int,
        sem: asyncio.Semaphore | None,
    ) -> list[JudgedSign]:
        """One panel member: judge the permuted evidence, return signs in **canonical** order.

        The presentation order is :func:`_permutation` of ``(hypothesis_id, sample_index)``; the
        model's per-``ref`` verdicts are un-permuted back to the input order before tallying. A
        missing ``ref`` (the model declined to classify an item) defaults to ``IRRELEVANT`` — the
        conservative reading (it asserted no bearing), keeping the per-item sample count uniform at
        ``N``; an out-of-range or duplicate ``ref`` is ignored (first verdict wins). The optional
        semaphore bounds the *individual* call so the panel shares the producer's global LLM budget
        (the same permit discipline as the verify fan-out).
        """
        order = _permutation(hypothesis_id, sample_index, len(evidence))
        presented = [evidence[i] for i in order]
        messages = self.build_messages(hypothesis_text, presented)
        if sem is not None:
            async with sem:
                raw = await self.llm.guided_complete(messages, JUDGE_SCHEMA, self.sampling)
        else:
            raw = await self.llm.guided_complete(messages, JUDGE_SCHEMA, self.sampling)
        verdicts = EdgeVerdicts.model_validate(raw).verdicts

        # ref is 1-based into `presented`; first verdict per ref wins, out-of-range ignored.
        by_presented: dict[int, JudgedSign] = {}
        for v in verdicts:
            pos = v.ref - 1
            if 0 <= pos < len(presented) and pos not in by_presented:
                by_presented[pos] = v.sign

        signs: list[JudgedSign] = [JudgedSign.IRRELEVANT] * len(evidence)
        for presented_pos, canonical_idx in enumerate(order):
            signs[canonical_idx] = by_presented.get(presented_pos, JudgedSign.IRRELEVANT)
        return signs

    async def judge_hypothesis(
        self,
        hypothesis_id: NodeId,
        hypothesis_text: str,
        evidence: Sequence[JudgeEvidence],
        *,
        sem: asyncio.Semaphore | None = None,
    ) -> HypothesisJudgment:
        """Adjudicate one hypothesis's candidate evidence into signed, calibrated edges (§8).

        Runs the ``n_samples`` blind panel members concurrently (each a permuted presentation),
        tallies the per-item votes, and folds them through the subjective-logic core: the dominant
        direction is the edge ``sign``; ``positive``/``negative`` are the agreeing/opposing
        directional votes and ``irrelevant`` votes abstain (raising uncertainty); the opinion is
        discounted by the item's source reliability and its projected probability is the edge
        ``strength``. An ``irrelevant`` **plurality** drops the pair (recorded in ``irrelevant``);
        a split direction is surfaced as ``sign_stable=False``. Empty evidence → an empty verdict.
        """
        if not evidence:
            return HypothesisJudgment(hypothesis=hypothesis_id)

        samples = await asyncio.gather(
            *(
                self._sample_signs(hypothesis_id, hypothesis_text, evidence, s, sem)
                for s in range(self.n_samples)
            )
        )

        judgments: list[EdgeJudgment] = []
        irrelevant: list[NodeId] = []
        for idx, item in enumerate(evidence):
            votes = [sample[idx] for sample in samples]
            supports = sum(1 for v in votes if v is JudgedSign.SUPPORTS)
            refutes = sum(1 for v in votes if v is JudgedSign.REFUTES)
            abstained = sum(1 for v in votes if v is JudgedSign.IRRELEVANT)

            # Drop iff irrelevant is the strict plurality — the panel mostly judged it not to bear.
            # A directional plurality (or a tie with irrelevant) survives, abstentions kept as
            # uncertainty: recall-leaning at the margin (precision is mostly done by now, §5.1).
            if abstained > supports and abstained > refutes:
                irrelevant.append(item.id)
                continue

            # Sign before magnitude: dominant direction is the edge type; the opposite-direction
            # votes are the disbelief, irrelevant votes abstain. A directional tie picks SUPPORTS
            # but is flagged unstable (sign split) — surfaced, not smoothed (§13).
            if supports >= refutes:
                sign, positive, negative = EdgeSign.SUPPORTS, supports, refutes
            else:
                sign, positive, negative = EdgeSign.REFUTES, refutes, supports
            sign_stable = min(supports, refutes) == 0

            # Multi-sample consistency → opinion → source-reliability discount → calibrated strength
            # (the §10 read-off; never the raw LLM number). abstentions widen uncertainty because
            # positive + negative < N leaves more prior mass.
            opinion = discount(opinion_from_evidence(positive, negative), item.reliability)
            judgments.append(
                EdgeJudgment(
                    evidence=item.id,
                    hypothesis=hypothesis_id,
                    sign=sign,
                    strength=opinion.projected_probability,
                    opinion=opinion,
                    positive=positive,
                    negative=negative,
                    abstained=abstained,
                    n_samples=self.n_samples,
                    sign_stable=sign_stable,
                )
            )

        return HypothesisJudgment(
            hypothesis=hypothesis_id,
            judgments=tuple(judgments),
            irrelevant=tuple(irrelevant),
        )
