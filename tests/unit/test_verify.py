"""Unit tests for the extract-then-verify NLI step (G1.4).

DB-free: the LLM is a mock. Covers the verdict contract, that verify_proposition maps a
guided-decode response to a verdict, that the prompt carries both the source span and the
proposition's claimed operators (so the verifier can judge preservation), and that the
prompt's entailment vocabulary is generated from the enum (no drift).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from iknos.core.cache import canonical_json_sha256, sha256_hex
from iknos.core.proposition import PropositionResult
from iknos.core.verify import VERIFY_SCHEMA, Verifier, VerifyVerdict, _VerifyOut
from iknos.types.epistemic import (
    Attribution,
    Entailment,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
)


def _prop(
    text: str,
    *,
    polarity: Polarity = Polarity.ASSERTED,
    modality: Modality = Modality.CATEGORICAL,
    attribution: Attribution = Attribution.DOCUMENT,
) -> PropositionResult:
    return PropositionResult(
        id=uuid.uuid4(),
        text=text,
        span_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        embedding=[0.0],
        polarity=polarity,
        modality=modality,
        attribution=attribution,
        scope="",
        epistemic_class=EpistemicClass.OBSERVATION,
        routing=Routing.FACT,
    )


def _verifier(verdict: dict) -> Verifier:
    llm = MagicMock()
    llm.model = "verifier-model"
    llm.guided_complete = AsyncMock(return_value={"verdicts": [verdict]})
    return Verifier(llm)


def test_verify_out_validates_full_record() -> None:
    out = _VerifyOut.model_validate(
        {
            "entailment": "contradicted",
            "polarity_preserved": False,
            "modality_preserved": True,
            "attribution_preserved": True,
        }
    )
    assert out.entailment is Entailment.CONTRADICTED
    assert out.polarity_preserved is False
    assert out.modality_preserved is True
    assert out.attribution_preserved is True


def test_verify_schema_is_built_from_the_contract() -> None:
    # The guided-decode schema is the contract's JSON schema (vLLM guided_json).
    assert VerifyVerdict.model_json_schema() == VERIFY_SCHEMA


@pytest.mark.asyncio
async def test_verify_proposition_maps_guided_response() -> None:
    v = _verifier(
        {
            "entailment": "entailed",
            "polarity_preserved": True,
            "modality_preserved": False,
            "attribution_preserved": True,
        }
    )
    verdict = await v.verify_proposition(
        "The bearing probably failed.", _prop("The bearing failed.")
    )
    assert verdict.entailment is Entailment.ENTAILED
    assert verdict.modality_preserved is False


@pytest.mark.asyncio
async def test_verify_messages_carry_span_and_proposition_operators() -> None:
    v = _verifier(
        {
            "entailment": "entailed",
            "polarity_preserved": True,
            "modality_preserved": True,
            "attribution_preserved": True,
        }
    )
    prop = _prop("The bearing failed.", polarity=Polarity.NEGATED, modality=Modality.PROBABLE)
    await v.verify_proposition("The bearing did not fail, per the operator.", prop)

    sent = v.llm.guided_complete.call_args.args[0]
    user = sent[1]["content"]
    # The verdict schema is what guided decoding is constrained to.
    assert v.llm.guided_complete.call_args.args[1] == VERIFY_SCHEMA
    # The source span text and the proposition's claimed operators both reach the model,
    # so it can judge whether polarity/modality were preserved.
    assert "The bearing did not fail, per the operator." in user
    assert "The bearing failed." in user
    assert Polarity.NEGATED.value in user
    assert Modality.PROBABLE.value in user


def test_system_prompt_lists_the_entailment_vocab() -> None:
    # Generated from the enum, never hand-typed (drift guard — guided decoding would hide it).
    for member in Entailment:
        assert member.value in Verifier.SYSTEM_PROMPT


# --- G1.15: verifier prompt/schema hashes feed the extractor's cache key ---


def test_verifier_prompt_sha_tracks_system_prompt() -> None:
    v = Verifier(MagicMock())
    h = v.prompt_sha()
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    # Hashes the instruction prompt: the realistic staleness case (a reword of the grading
    # instructions) moves the digest, re-deriving faithfulness instead of replaying a stale verdict.
    assert h == sha256_hex(Verifier.SYSTEM_PROMPT)


def test_verifier_schema_sha_is_canonical() -> None:
    v = Verifier(MagicMock())
    assert v.schema_sha() == canonical_json_sha256(VERIFY_SCHEMA)
