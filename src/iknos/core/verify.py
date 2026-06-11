"""Extract-then-verify NLI step (Phase 1, G1.4) — the faithfulness verifier (§3.1).

Provenance proves a proposition traces to a span; it does *not* prove the proposition
faithfully represents that span. This step closes that gap: an **independent verifier**
(a different model family from the extractor, §13 — cuts correlated error) reads the source
span and judges whether it entails the proposition *with its polarity and modality*. The
verdict drives the derived ``faithfulness`` score (G1.5,
:func:`~iknos.types.epistemic.faithfulness_from_verdict`), catching both hallucinated content
(not in the source) and silent operator distortion (a dropped negation, a flattened hedge).

The verifier never emits a number — it emits the categorical NLI judgement; faithfulness is
*derived* from it (§3.1: "confidence comes from consistency and verification, not verbalized
self-report"). It reuses :class:`~iknos.core.llm.LLMClient` verbatim, pointed at the verifier
endpoint, and has no DB access — so it runs in the propositionizer's concurrent phase.
"""

from typing import Any

from pydantic import BaseModel

from iknos.core.cache import canonical_json_sha256, sha256_hex
from iknos.core.llm import LLMClient
from iknos.core.prompts import vocab
from iknos.core.proposition import PropositionResult
from iknos.types.epistemic import Entailment


class _VerifyOut(BaseModel):
    """One verifier verdict (drives guided decoding).

    Categorical, never a number. ``attribution_preserved`` is emitted for audit but does
    **not** feed the faithfulness score this increment — attribution is a credibility signal
    (§9.1), not content-faithfulness; emitting it now lets a later increment wire it in
    without a contract change.
    """

    entailment: Entailment
    polarity_preserved: bool
    modality_preserved: bool
    attribution_preserved: bool


class VerifyVerdict(BaseModel):
    """Structured output contract; drives vLLM guided decoding.

    A list of one is used today (one proposition per call). The list shape leaves per-span
    batching open as a later optimization without a contract change.
    """

    verdicts: list[_VerifyOut]


VERIFY_SCHEMA = VerifyVerdict.model_json_schema()

# A *semantic* version of the verifier's output shape. Since G1.15 the verifier signature also
# carries prompt_sha/schema_sha (Verifier.prompt_sha/schema_sha), so a reworded SYSTEM_PROMPT or
# changed VERIFY_SCHEMA re-derives faithfulness without a bump here; keep bumping it for a
# deliberate "verifier contract changed" marker (e.g. a user-template reword the prompt_sha does
# not cover). Folded into the extraction cache key (G1.7) via the verifier signature. Mirrors
# core/ingest.py::SEGMENT_SCHEMA_VERSION.
VERIFY_SCHEMA_VERSION = 1


class Verifier:
    """Judges whether a source span entails a proposition with its operators preserved (G1.4)."""

    # Surfaced on the instance so the propositionizer can fold the verifier's version into its
    # extraction cache key (G1.7) without a circular import back into verify.py.
    SCHEMA_VERSION = VERIFY_SCHEMA_VERSION

    # Generated from the enum (not hand-typed) so the prompt vocabulary can never drift from
    # the guided-decode schema — exactly the discipline the extractor's prompt follows.
    SYSTEM_PROMPT = (
        "You are a strict verifier. You are given a SOURCE passage and one PROPOSITION that "
        "was extracted from it, with the epistemic operators the extractor claims. Judge the "
        "PROPOSITION against the SOURCE ALONE — never world knowledge — and report whether the "
        "source supports it and whether each operator was preserved.\n"
        "The proposition's `text` holds AFFIRMATIVE content; its `polarity` carries the sign "
        '(a denial is stored as affirmative text + polarity=negated, e.g. "the bearing did not '
        'fail" -> text "The bearing failed." + polarity negated). Judge polarity against that '
        "convention, not the surface wording.\n"
        "Fields:\n"
        f"- entailment ({vocab(Entailment)}): `entailed` = the source supports the "
        "proposition's content; `neutral` = the source neither supports nor contradicts it "
        "(content absent from the source / hallucinated); `contradicted` = the source asserts "
        "the opposite.\n"
        "- polarity_preserved: false if the claimed polarity disagrees with the source (a "
        "dropped or added negation — a sign flip).\n"
        "- modality_preserved: false if the claimed certainty disagrees with the source (e.g. "
        'the source hedges "probably" but the proposition is categorical).\n'
        "- attribution_preserved: false if the claimed attribution disagrees with the source "
        "(e.g. a named source's claim ingested as the document's own).\n"
        'Example: SOURCE "The operator said the bearing probably did not fail." PROPOSITION '
        'text "The bearing failed." polarity=asserted modality=categorical '
        "attribution=document -> "
        '{"entailment": "contradicted", "polarity_preserved": false, '
        '"modality_preserved": false, "attribution_preserved": false} '
        "(the source denies it, hedges it, and attributes it to a named source).\n"
        'Return JSON of the form {"verdicts": [{"entailment": "...", "polarity_preserved": '
        'true, "modality_preserved": true, "attribution_preserved": true}]}.'
    )

    def __init__(self, llm: LLMClient, *, sampling: dict[str, Any] | None = None) -> None:
        self.llm = llm
        self.sampling = sampling or {"temperature": 0.0}

    def build_messages(self, span_text: str, prop: PropositionResult) -> list[dict[str, str]]:
        """Assemble the chat messages for verifying one proposition against its source span."""
        user = (
            f"SOURCE:\n{span_text}\n\n"
            "PROPOSITION:\n"
            f"- text: {prop.text}\n"
            f"- polarity: {prop.polarity}\n"
            f"- modality: {prop.modality}\n"
            f"- attribution: {prop.attribution}"
        )
        return [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def prompt_sha(self) -> str:
        """SHA-256 of the verifier's instruction prompt (G1.15) — folded into the extractor's
        cache key via the verifier signature.

        Hashes ``SYSTEM_PROMPT``, where every grading instruction (and the interpolated enum
        vocabulary) lives. The user message (:meth:`build_messages`) is pure field interpolation of
        the proposition under test — it carries no wording that shifts the verdict independently of
        the system prompt — so it is excluded; a reword of the instructions, the realistic
        staleness case, moves this digest. ``VERIFY_SCHEMA_VERSION`` remains the manual lever for a
        deliberate user-template change. Symmetric with the extractor's ``prompt_sha`` (G1.15).
        """
        return sha256_hex(self.SYSTEM_PROMPT)

    def schema_sha(self) -> str:
        """SHA-256 of the canonical verifier output schema (G1.15) — key-order-insensitive."""
        return canonical_json_sha256(VERIFY_SCHEMA)

    async def verify_proposition(self, span_text: str, prop: PropositionResult) -> _VerifyOut:
        """One verify LLM call for one proposition. No DB access (concurrent-phase safe)."""
        raw = await self.llm.guided_complete(
            self.build_messages(span_text, prop), VERIFY_SCHEMA, self.sampling
        )
        return VerifyVerdict.model_validate(raw).verdicts[0]
