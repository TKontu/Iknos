"""Unit tests for the blind, randomized, multi-sample edge judge (G4.3 slice 2).

DB-free: the LLM is a fake whose per-call response is scripted, so the tests pin the *judging*
logic — the §8 disciplines — independently of any model. Covers: the sign-only output contract
(no magnitude field); blindness (no hypothesis state in the prompt); the deterministic per-sample
permutation (position-bias guard) and that signs are un-permuted back to the canonical item; the
multi-sample-consistency → ``(positive, negative)`` → strength fold; the irrelevant-plurality drop;
the sign-instability finding; source-reliability discounting; and the robustness of the ref mapping
(missing / out-of-range / duplicate refs).
"""

import re
from collections.abc import Callable

import pytest

from iknos.core.cache import canonical_json_sha256, sha256_hex
from iknos.core.edge_judge import (
    DEFAULT_JUDGE_SAMPLES,
    JUDGE_SCHEMA,
    EdgeJudge,
    JudgedSign,
    JudgeEvidence,
    _permutation,
)
from iknos.core.subjective_logic import discount, opinion_from_evidence
from iknos.types.edges import EdgeSign

# --- fake LLM ---------------------------------------------------------------------------------

_EVIDENCE_LINE = re.compile(r"^(\d+)\. (.*)$")


def _presented_texts(messages: list[dict[str, str]]) -> list[str]:
    """The evidence texts in the order the prompt presented them (parsing the user message)."""
    user = messages[1]["content"]
    block = user.split("EVIDENCE:\n", 1)[1]
    return [m.group(2) for line in block.splitlines() if (m := _EVIDENCE_LINE.match(line))]


class FakeLLM:
    """A scripted stand-in for :class:`~iknos.core.llm.LLMClient`.

    ``responder(presented_texts, call_index)`` returns the raw guided-decode dict for one sample;
    every call's presented order is recorded so a test can assert the permutation actually varied.
    """

    def __init__(
        self, responder: Callable[[list[str], int], dict], *, model: str = "judge-model"
    ) -> None:
        self.model = model
        self._responder = responder
        self.presented_orders: list[tuple[str, ...]] = []
        self.call_count = 0

    async def guided_complete(
        self, messages: list[dict[str, str]], json_schema: dict, sampling: dict | None = None
    ) -> dict:
        texts = _presented_texts(messages)
        self.presented_orders.append(tuple(texts))
        resp = self._responder(texts, self.call_count)
        self.call_count += 1
        return resp


def _by_text(mapping: dict[str, JudgedSign]) -> Callable[[list[str], int], dict]:
    """Responder that classifies each item by its TEXT regardless of presented position — so a
    correct un-permute recovers each canonical item's sign no matter the order it was shown in."""

    def responder(texts: list[str], _i: int) -> dict:
        return {"verdicts": [{"ref": p + 1, "sign": mapping[t].value} for p, t in enumerate(texts)]}

    return responder


def _per_sample(signs: list[JudgedSign]) -> Callable[[list[str], int], dict]:
    """Responder for a SINGLE-item hypothesis (permutation is identity): sample ``i`` votes
    ``signs[i]``."""

    def responder(texts: list[str], i: int) -> dict:
        assert len(texts) == 1, "use _per_sample only for single-evidence hypotheses"
        return {"verdicts": [{"ref": 1, "sign": signs[i].value}]}

    return responder


def _evi(node_id: str, text: str, reliability: float = 1.0) -> JudgeEvidence:
    return JudgeEvidence(id=node_id, text=text, reliability=reliability)


# --- output contract: sign before magnitude --------------------------------------------------


def test_judged_sign_maps_to_edge_sign() -> None:
    assert JudgedSign.SUPPORTS.to_edge_sign() is EdgeSign.SUPPORTS
    assert JudgedSign.REFUTES.to_edge_sign() is EdgeSign.REFUTES


def test_irrelevant_has_no_edge_sign() -> None:
    with pytest.raises(ValueError, match="no edge sign"):
        JudgedSign.IRRELEVANT.to_edge_sign()


def test_schema_emits_sign_only_no_magnitude() -> None:
    # §8: the model classifies direction, never a number. The verdict has exactly ref + sign.
    verdict_props = JUDGE_SCHEMA["$defs"]["_EdgeVerdict"]["properties"]
    assert set(verdict_props) == {"ref", "sign"}
    assert not {"strength", "magnitude", "score", "confidence", "weight"} & set(verdict_props)
    # `sign` is constrained to the three-way enum; no numeric magnitude is anywhere in the contract.
    sign_values = set(JUDGE_SCHEMA["$defs"]["JudgedSign"]["enum"])
    assert sign_values == {"supports", "refutes", "irrelevant"}


# --- blindness (sycophancy guard) -------------------------------------------------------------


def test_prompt_is_blind_to_hypothesis_state() -> None:
    judge = EdgeJudge(FakeLLM(_by_text({})))
    messages = judge.build_messages("The bearing failed.", [_evi("e1", "Vibration spiked.")])
    user = messages[1]["content"]
    # The user message carries the claim + evidence and NOTHING about the hypothesis's current
    # acceptability/state — the judge cannot anchor on what it is never shown.
    assert "The bearing failed." in user
    assert "Vibration spiked." in user
    for leak in ("acceptability", "accepted", "current state", "supported", "refuted", "verdict"):
        assert leak not in user.lower()


def test_evidence_is_numbered_one_based() -> None:
    judge = EdgeJudge(FakeLLM(_by_text({})))
    messages = judge.build_messages("H", [_evi("a", "first"), _evi("b", "second")])
    assert "1. first" in messages[1]["content"]
    assert "2. second" in messages[1]["content"]


def test_prompt_sha_and_schema_sha_are_stable_digests() -> None:
    judge = EdgeJudge(FakeLLM(_by_text({})))
    assert judge.prompt_sha() == sha256_hex(judge.SYSTEM_PROMPT)
    assert judge.schema_sha() == canonical_json_sha256(JUDGE_SCHEMA)


# --- the permutation: deterministic, replayable, varies per sample ----------------------------


def test_permutation_is_deterministic_and_replayable() -> None:
    a = _permutation("hyp-1", 3, 6)
    b = _permutation("hyp-1", 3, 6)
    assert a == b  # same (hypothesis, sample) → same order across runs (auditable)
    assert sorted(a) == list(range(6))  # a genuine permutation


def test_permutation_varies_across_samples_and_hypotheses() -> None:
    orders = {tuple(_permutation("hyp-1", s, 6)) for s in range(5)}
    assert len(orders) > 1  # the position-bias guard actually moves items
    assert tuple(_permutation("hyp-1", 0, 6)) != tuple(_permutation("hyp-2", 0, 6))


def test_permutation_identity_for_trivial_lengths() -> None:
    assert _permutation("h", 0, 0) == []
    assert _permutation("h", 0, 1) == [0]


# --- multi-sample consistency → strength ------------------------------------------------------


@pytest.mark.asyncio
async def test_unanimous_support_high_strength() -> None:
    n = 5
    judge = EdgeJudge(FakeLLM(_per_sample([JudgedSign.SUPPORTS] * n)), n_samples=n)
    result = await judge.judge_hypothesis("H", "H", [_evi("e1", "x")])
    assert len(result.judgments) == 1
    j = result.judgments[0]
    assert j.sign is EdgeSign.SUPPORTS
    assert (j.positive, j.negative, j.abstained) == (5, 0, 0)
    assert j.sign_stable is True
    assert j.n_samples == 5
    expected = opinion_from_evidence(5, 0).projected_probability
    assert j.strength == pytest.approx(expected)
    assert j.strength > 0.8


@pytest.mark.asyncio
async def test_unanimous_refute_sets_refutes_sign() -> None:
    n = 4
    judge = EdgeJudge(FakeLLM(_per_sample([JudgedSign.REFUTES] * n)), n_samples=n)
    result = await judge.judge_hypothesis("H", "H", [_evi("e1", "x")])
    j = result.judgments[0]
    assert j.sign is EdgeSign.REFUTES
    assert (j.positive, j.negative) == (4, 0)
    assert j.sign_stable is True


@pytest.mark.asyncio
async def test_split_sign_is_flagged_unstable_and_near_neutral() -> None:
    # 3 supports, 2 refutes: a directional split — surfaced (sign_stable=False), strength pulled
    # toward the base rate, never smoothed into false confidence (§13).
    signs = [JudgedSign.SUPPORTS] * 3 + [JudgedSign.REFUTES] * 2
    judge = EdgeJudge(FakeLLM(_per_sample(signs)), n_samples=5)
    j = (await judge.judge_hypothesis("H", "H", [_evi("e1", "x")])).judgments[0]
    assert j.sign is EdgeSign.SUPPORTS  # plurality direction
    assert (j.positive, j.negative) == (3, 2)
    assert j.sign_stable is False
    assert 0.45 < j.strength < 0.65


@pytest.mark.asyncio
async def test_abstentions_lower_strength_versus_full_agreement() -> None:
    # Both panels are N=5 and never vote against; the second has 2 abstentions. Irrelevant votes
    # are "neither", so they leave more prior mass → more uncertainty → lower strength.
    unanimous = EdgeJudge(FakeLLM(_per_sample([JudgedSign.SUPPORTS] * 5)), n_samples=5)
    abstaining = EdgeJudge(
        FakeLLM(_per_sample([JudgedSign.SUPPORTS] * 3 + [JudgedSign.IRRELEVANT] * 2)), n_samples=5
    )
    ju = (await unanimous.judge_hypothesis("H", "H", [_evi("e", "x")])).judgments[0]
    ja = (await abstaining.judge_hypothesis("H", "H", [_evi("e", "x")])).judgments[0]
    assert ja.abstained == 2
    assert ja.positive == 3
    assert ja.strength < ju.strength


# --- the irrelevant-plurality drop (recall → precision handoff) -------------------------------


@pytest.mark.asyncio
async def test_irrelevant_plurality_drops_the_pair() -> None:
    # 3 irrelevant, 1 support, 1 refute: irrelevant is the strict plurality → no edge written.
    signs = [JudgedSign.IRRELEVANT] * 3 + [JudgedSign.SUPPORTS, JudgedSign.REFUTES]
    judge = EdgeJudge(FakeLLM(_per_sample(signs)), n_samples=5)
    result = await judge.judge_hypothesis("H", "H", [_evi("e1", "x")])
    assert result.judgments == ()
    assert result.irrelevant == ("e1",)


@pytest.mark.asyncio
async def test_directional_plurality_survives_with_abstentions_as_uncertainty() -> None:
    # 2 support, 1 refute, 2 irrelevant: a direction is the plurality (irrelevant is not strictly
    # greater than supports), so it survives — recall-leaning at the margin.
    signs = [JudgedSign.SUPPORTS] * 2 + [JudgedSign.REFUTES] + [JudgedSign.IRRELEVANT] * 2
    judge = EdgeJudge(FakeLLM(_per_sample(signs)), n_samples=5)
    result = await judge.judge_hypothesis("H", "H", [_evi("e1", "x")])
    assert len(result.judgments) == 1
    j = result.judgments[0]
    assert j.sign is EdgeSign.SUPPORTS
    assert (j.positive, j.negative, j.abstained) == (2, 1, 2)
    assert j.sign_stable is False


# --- source-reliability discounting (§8 ↔ §9.1) ----------------------------------------------


@pytest.mark.asyncio
async def test_low_reliability_discounts_strength() -> None:
    signs = [JudgedSign.SUPPORTS] * 5
    full = EdgeJudge(FakeLLM(_per_sample(signs)), n_samples=5)
    half = EdgeJudge(FakeLLM(_per_sample(signs)), n_samples=5)
    jf = (await full.judge_hypothesis("H", "H", [_evi("e", "x", reliability=1.0)])).judgments[0]
    jh = (await half.judge_hypothesis("H", "H", [_evi("e", "x", reliability=0.5)])).judgments[0]
    assert jh.strength < jf.strength
    expected = discount(opinion_from_evidence(5, 0), 0.5).projected_probability
    assert jh.strength == pytest.approx(expected)


@pytest.mark.asyncio
async def test_zero_reliability_yields_base_rate_strength() -> None:
    # A wholly untrusted source → vacuous opinion → projects to the base rate (0.5), asserting
    # nothing rather than lending its raw vote.
    judge = EdgeJudge(FakeLLM(_per_sample([JudgedSign.SUPPORTS] * 5)), n_samples=5)
    j = (await judge.judge_hypothesis("H", "H", [_evi("e", "x", reliability=0.0)])).judgments[0]
    assert j.strength == pytest.approx(0.5)


# --- un-permute correctness + randomization (the position-bias guard, end to end) -------------


@pytest.mark.asyncio
async def test_signs_are_unpermuted_back_to_canonical_items() -> None:
    # Each item is classified by its TEXT regardless of shown position; a correct un-permute
    # recovers each canonical item's sign across all the (differently-ordered) samples.
    mapping = {
        "alpha": JudgedSign.SUPPORTS,
        "beta": JudgedSign.REFUTES,
        "gamma": JudgedSign.IRRELEVANT,
    }
    fake = FakeLLM(_by_text(mapping))
    judge = EdgeJudge(fake, n_samples=6)
    evidence = [_evi("a", "alpha"), _evi("b", "beta"), _evi("c", "gamma")]
    result = await judge.judge_hypothesis("hyp-x", "H", evidence)

    by_id = {j.evidence: j for j in result.judgments}
    assert by_id["a"].sign is EdgeSign.SUPPORTS
    assert by_id["a"].positive == 6  # unanimous despite each sample being a different order
    assert by_id["b"].sign is EdgeSign.REFUTES
    assert "c" in result.irrelevant  # gamma judged irrelevant in every sample → dropped
    # The guard actually fired: the 6 samples did not all use the same presented order.
    assert len(set(fake.presented_orders)) > 1
    # Every recorded order is a genuine permutation of the three items.
    for order in fake.presented_orders:
        assert sorted(order) == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_judgments_follow_input_evidence_order() -> None:
    mapping = {"a": JudgedSign.SUPPORTS, "b": JudgedSign.SUPPORTS, "c": JudgedSign.SUPPORTS}
    judge = EdgeJudge(FakeLLM(_by_text(mapping)), n_samples=3)
    evidence = [_evi("n1", "a"), _evi("n2", "b"), _evi("n3", "c")]
    result = await judge.judge_hypothesis("h", "H", evidence)
    assert [j.evidence for j in result.judgments] == ["n1", "n2", "n3"]


# --- robustness of the ref → item mapping -----------------------------------------------------


@pytest.mark.asyncio
async def test_missing_ref_counts_as_irrelevant_abstention() -> None:
    # Sample 0 classifies both; sample 1 omits item 2 → that sample abstains on item 2.
    def responder(texts: list[str], i: int) -> dict:
        if i == 0:
            return {"verdicts": [{"ref": 1, "sign": "supports"}, {"ref": 2, "sign": "supports"}]}
        return {"verdicts": [{"ref": 1, "sign": "supports"}]}

    judge = EdgeJudge(FakeLLM(responder), n_samples=2)
    # Single-item hypotheses (identity permutation) so ref positions are stable.
    result = await judge.judge_hypothesis("h", "H", [_evi("e1", "a"), _evi("e2", "b")])
    by_id = {j.evidence: j for j in result.judgments}
    assert by_id["e2"].abstained == 1  # the omitted verdict became an abstention, not a crash
    assert by_id["e2"].positive == 1


@pytest.mark.asyncio
async def test_out_of_range_and_duplicate_refs_are_ignored() -> None:
    def responder(texts: list[str], i: int) -> dict:
        return {
            "verdicts": [
                {"ref": 1, "sign": "supports"},  # first verdict for ref 1 wins
                {"ref": 1, "sign": "refutes"},  # duplicate ignored
                {"ref": 99, "sign": "supports"},  # out of range ignored
            ]
        }

    judge = EdgeJudge(FakeLLM(responder), n_samples=1)
    j = (await judge.judge_hypothesis("h", "H", [_evi("e1", "a")])).judgments[0]
    assert j.sign is EdgeSign.SUPPORTS
    assert (j.positive, j.negative) == (1, 0)


# --- misc contracts ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_evidence_returns_empty_verdict() -> None:
    judge = EdgeJudge(FakeLLM(_by_text({})))
    result = await judge.judge_hypothesis("h", "H", [])
    assert result.hypothesis == "h"
    assert result.judgments == ()
    assert result.irrelevant == ()


def test_rejects_non_positive_sample_count() -> None:
    with pytest.raises(ValueError, match="n_samples must be >= 1"):
        EdgeJudge(FakeLLM(_by_text({})), n_samples=0)


def test_default_sample_count() -> None:
    assert EdgeJudge(FakeLLM(_by_text({}))).n_samples == DEFAULT_JUDGE_SAMPLES
