"""Unit tests for the evidential-edge producer (G4.3 slice 3).

DB-free: the pure core — the significance policy, the edge-property flattening, the per-hypothesis
evidence grouping, and the per-hypothesis *write plan* — is tested with hand-built rows; the
``produce`` orchestration is exercised with a fake LLM, a fake candidate adapter, and monkeypatched
``merge_edge`` / ``record_action`` so the read→judge→write wiring is pinned without a real graph
(the live-AGE path is the integration test). Covers: significance = tier_weight·credibility (with
the unknown-credibility identity and bounds), the edge props (sign/strength/significance,
``sign_stable``, open bitemporal), the §8/§9 reconciliation (judge fed identity reliability,
credibility routed to significance), the grouping (drop nodes with no text, replayable order), the
plan (label, Action provenance incl. dropped-irrelevant + shas), and the end-to-end fold (findings,
Action count, the irrelevant drop).
"""

import uuid
from datetime import UTC, datetime

import pytest

from iknos.core.candidates import Candidate, CandidatePool, CandidateSource
from iknos.core.edge_judge import (
    EdgeJudge,
    EdgeJudgment,
    HypothesisJudgment,
    JudgedSign,
)
from iknos.core.edge_producer import (
    DEFAULT_SIGNIFICANCE,
    PRODUCER_ACTION_TYPE,
    PRODUCER_ACTOR,
    EdgeProducer,
    EdgeProductionResult,
    NodeMeta,
    ProducedEdge,
    SignificancePolicy,
    build_evidence,
    edge_significance,
    evidential_edge_props,
    plan_hypothesis,
)
from iknos.core.subjective_logic import Opinion
from iknos.types.edges import EdgeSign
from iknos.types.nodes import Tier

# A timestamp the tests can assert against without importing the real clock.
NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _judgment(
    evidence: str,
    hypothesis: str,
    *,
    sign: EdgeSign = EdgeSign.SUPPORTS,
    strength: float = 0.8,
    sign_stable: bool = True,
    positive: int = 5,
    negative: int = 0,
    abstained: int = 0,
) -> EdgeJudgment:
    """A hand-built :class:`EdgeJudgment` (the opinion is a placeholder — only scalars matter)."""
    return EdgeJudgment(
        evidence=evidence,
        hypothesis=hypothesis,
        sign=sign,
        strength=strength,
        opinion=Opinion(belief=strength, disbelief=0.0, uncertainty=1.0 - strength),
        positive=positive,
        negative=negative,
        abstained=abstained,
        n_samples=positive + negative + abstained,
        sign_stable=sign_stable,
    )


# --- significance policy (§9) -----------------------------------------------------------------


def test_default_significance_is_credibility_uniform_tier() -> None:
    # The MVP: uniform tier weight (1.0) ⇒ significance is exactly the credibility term.
    assert edge_significance(DEFAULT_SIGNIFICANCE, Tier.CASE, 0.6) == pytest.approx(0.6)
    assert edge_significance(DEFAULT_SIGNIFICANCE, Tier.REFERENCE, 1.0) == pytest.approx(1.0)


def test_significance_unknown_credibility_is_identity() -> None:
    # Credibility undefined (incomplete source chain) reads as 1.0 — undefined, not zero (§9.1).
    assert edge_significance(DEFAULT_SIGNIFICANCE, Tier.CASE, None) == pytest.approx(1.0)


def test_significance_multiplies_tier_weight_and_credibility() -> None:
    policy = SignificancePolicy(tier_weight={Tier.WORKING: 0.5}, default_tier_weight=1.0)
    assert edge_significance(policy, Tier.WORKING, 0.8) == pytest.approx(0.4)
    # A tier absent from the map falls back to the default weight.
    assert edge_significance(policy, Tier.CASE, 0.8) == pytest.approx(0.8)
    # An unknown tier (None) also takes the default weight.
    assert edge_significance(policy, None, 0.5) == pytest.approx(0.5)


def test_significance_is_clamped_and_rejects_out_of_range_credibility() -> None:
    # A tier weight > 1 cannot push significance above 1.
    policy = SignificancePolicy(tier_weight={Tier.SCHEMA: 2.0})
    assert edge_significance(policy, Tier.SCHEMA, 0.9) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="credibility"):
        edge_significance(DEFAULT_SIGNIFICANCE, Tier.CASE, 1.5)


# --- edge props -------------------------------------------------------------------------------


def test_evidential_edge_props_carries_the_three_quantities_and_open_bitemporal() -> None:
    props = evidential_edge_props(
        box="box-1",
        sign=EdgeSign.REFUTES,
        strength=0.42,
        significance=0.7,
        sign_stable=False,
        now=NOW,
    )
    assert props["sign"] == "refutes"  # categorical, lowercase EdgeSign value
    assert props["strength"] == 0.42
    assert props["significance"] == 0.7
    assert props["sign_stable"] is False  # the §13 finding is graph-queryable
    assert props["box"] == "box-1"
    # Stamped open so a retraction can stamp valid_to and the QBAF current-state filter drops it.
    assert props["valid_to"] is None
    assert props["event_time"] is None
    assert props["valid_from"] == NOW.isoformat()
    assert props["ingested_at"] == NOW.isoformat()


# --- evidence grouping ------------------------------------------------------------------------


def _meta(statement: str, tier: Tier | None = Tier.CASE, box: str | None = "box-1") -> NodeMeta:
    return NodeMeta(statement=statement, tier=tier, box=box)


def test_build_evidence_groups_by_hypothesis_with_identity_reliability() -> None:
    pool = CandidatePool(
        candidates=(
            Candidate(
                evidence="e2", hypothesis="h1", sources=frozenset({CandidateSource.EMBEDDING_KNN})
            ),
            Candidate(
                evidence="e1",
                hypothesis="h1",
                sources=frozenset({CandidateSource.STRUCTURAL_ENTITY}),
            ),
            Candidate(
                evidence="e3", hypothesis="h2", sources=frozenset({CandidateSource.EMBEDDING_KNN})
            ),
        )
    )
    meta = {
        "h1": _meta("hypothesis one"),
        "h2": _meta("hypothesis two"),
        "e1": _meta("ev one"),
        "e2": _meta("ev two"),
        "e3": _meta("ev three"),
    }
    grouped = build_evidence(pool, meta)

    assert set(grouped) == {"h1", "h2"}
    h1_text, h1_ev = grouped["h1"]
    assert h1_text == "hypothesis one"
    # Sorted by id for a replayable permutation seed, regardless of pool order.
    assert [e.id for e in h1_ev] == ["e1", "e2"]
    # Reliability is the identity 1.0 — credibility is routed to significance, not the judge.
    assert all(e.reliability == 1.0 for e in h1_ev)
    assert [e.text for e in h1_ev] == ["ev one", "ev two"]


def test_build_evidence_drops_nodes_without_resolved_text() -> None:
    pool = CandidatePool(
        candidates=(
            Candidate(
                evidence="e1", hypothesis="h1", sources=frozenset({CandidateSource.EMBEDDING_KNN})
            ),
            Candidate(
                evidence="missing",
                hypothesis="h1",
                sources=frozenset({CandidateSource.EMBEDDING_KNN}),
            ),
            Candidate(
                evidence="e1",
                hypothesis="missing-hyp",
                sources=frozenset({CandidateSource.EMBEDDING_KNN}),
            ),
        )
    )
    meta = {"h1": _meta("hyp"), "e1": _meta("ev")}
    grouped = build_evidence(pool, meta)

    # Candidate against an unknown hypothesis is dropped; evidence with no text is dropped.
    assert set(grouped) == {"h1"}
    assert [e.id for e in grouped["h1"][1]] == ["e1"]


# --- per-hypothesis plan ----------------------------------------------------------------------


def _plan(judgment: HypothesisJudgment, meta: dict[str, NodeMeta], cred: dict[str, float | None]):
    return plan_hypothesis(
        judgment,
        node_meta=meta,
        credibility=cred,
        policy=DEFAULT_SIGNIFICANCE,
        now=NOW,
        model="judge-model",
        sampling={"temperature": 0.0},
        prompt_sha="psha",
        schema_sha="ssha",
        schema_version=1,
    )


def test_plan_hypothesis_builds_signed_edges_with_routed_significance() -> None:
    judgment = HypothesisJudgment(
        hypothesis="h1",
        judgments=(
            _judgment("e1", "h1", sign=EdgeSign.SUPPORTS, strength=0.8),
            _judgment(
                "e2",
                "h1",
                sign=EdgeSign.REFUTES,
                strength=0.3,
                positive=3,
                negative=2,
                sign_stable=False,
            ),
        ),
        irrelevant=("e3",),
    )
    meta = {
        "h1": _meta("hyp", box="box-h"),
        "e1": _meta("ev1", tier=Tier.CASE),
        "e2": _meta("ev2", tier=Tier.WORKING),
    }
    cred = {"e1": 0.5, "e2": None}
    plan = _plan(judgment, meta, cred)

    assert plan.hypothesis == "h1"
    assert [e.label for e in plan.edges] == ["SUPPORTS", "REFUTES"]
    # Edge inherits the TARGET hypothesis's box, runs evidence -> hypothesis.
    assert all(e.dst_id == "h1" and e.props["box"] == "box-h" for e in plan.edges)
    e1, e2 = plan.edges
    assert e1.src_id == "e1" and e1.props["strength"] == 0.8
    # significance = tier_weight(1.0) * credibility(0.5) = 0.5 (credibility routed here, not str.)
    assert e1.props["significance"] == pytest.approx(0.5)
    # e2: unknown credibility -> identity, significance = 1.0; sign split surfaced on the edge.
    assert e2.props["significance"] == pytest.approx(1.0)
    assert e2.props["sign_stable"] is False


def test_plan_hypothesis_action_records_provenance_and_drops() -> None:
    judgment = HypothesisJudgment(
        hypothesis="h1",
        judgments=(_judgment("e1", "h1", positive=4, negative=1, abstained=0),),
        irrelevant=("e3", "e4"),
    )
    meta = {"h1": _meta("hyp"), "e1": _meta("ev1")}
    plan = _plan(judgment, meta, {"e1": 0.9})

    a = plan.action
    assert a.actor == PRODUCER_ACTOR and a.action_type == PRODUCER_ACTION_TYPE
    assert a.model == "judge-model"
    # Inputs: every candidate considered (survivors + dropped) + the pipeline shas/version.
    assert a.inputs["hypothesis"] == "h1"
    assert a.inputs["candidates"] == ["e1", "e3", "e4"]
    assert a.inputs["prompt_sha"] == "psha"
    assert a.inputs["schema_sha"] == "ssha"
    assert a.inputs["schema_version"] == 1
    # Outputs: the written edges, the per-edge vote audit, and the auditable irrelevant drop.
    assert a.outputs["edges"] == ["e1->h1"]
    assert a.outputs["dropped_irrelevant"] == ["e3", "e4"]
    (audit,) = a.outputs["judgments"]
    assert audit == {
        "evidence": "e1",
        "sign": "supports",
        "strength": 0.8,
        "significance": pytest.approx(0.9),
        "positive": 4,
        "negative": 1,
        "abstained": 0,
        "n_samples": 5,
        "sign_stable": True,
    }


# --- result helpers ---------------------------------------------------------------------------


def test_result_surfaces_unstable_edges_as_findings() -> None:
    stable = ProducedEdge("e1", "h1", EdgeSign.SUPPORTS, 0.8, 0.9, sign_stable=True)
    unstable = ProducedEdge("e2", "h1", EdgeSign.REFUTES, 0.3, 0.5, sign_stable=False)
    res = EdgeProductionResult(edges=(stable, unstable))
    assert res.unstable == (unstable,)
    assert res.is_finding is True
    assert EdgeProductionResult(edges=(stable,)).is_finding is False


# --- produce orchestration (fake I/O) ---------------------------------------------------------


class _FakeLLM:
    """Scripts each evidence item's sign by its text (un-permute-robust), like the slice-2 tests."""

    def __init__(self, by_text: dict[str, JudgedSign], *, model: str = "judge-model") -> None:
        self.model = model
        self._by_text = by_text

    async def guided_complete(self, messages, json_schema, sampling=None) -> dict:  # noqa: ANN001
        block = messages[1]["content"].split("EVIDENCE:\n", 1)[1]
        verdicts = []
        for line in block.splitlines():
            num, _, text = line.partition(". ")
            verdicts.append({"ref": int(num), "sign": self._by_text[text].value})
        return {"verdicts": verdicts}


class _FakeCandidateAdapter:
    def __init__(self, pool: CandidatePool) -> None:
        self._pool = pool

    async def generate(self, session, *, strategy=None, k=10) -> CandidatePool:  # noqa: ANN001
        return self._pool


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class _StubProducer(EdgeProducer):
    """Overrides the two metadata reads so ``produce`` runs without a graph."""

    def __init__(self, *args, node_meta, credibility, **kwargs) -> None:  # noqa: ANN002
        super().__init__(*args, **kwargs)
        self._node_meta = node_meta
        self._credibility = credibility

    async def _load_node_meta(self, session):  # noqa: ANN001
        return self._node_meta

    async def _load_credibility(self, session, evidence_ids):  # noqa: ANN001
        return {e: self._credibility.get(e) for e in evidence_ids}


@pytest.mark.asyncio
async def test_produce_judges_writes_edges_and_records_actions(monkeypatch) -> None:  # noqa: ANN001
    writes: list[dict] = []
    actions: list[dict] = []

    async def fake_merge_edge(session, *, src_id, dst_id, label, props):  # noqa: ANN001
        writes.append({"src": src_id, "dst": dst_id, "label": label, "props": props})

    async def fake_record_action(session, **kw):  # noqa: ANN001
        actions.append(kw)
        return uuid.uuid4()

    monkeypatch.setattr("iknos.db.age.merge_edge", fake_merge_edge)
    monkeypatch.setattr("iknos.provenance.action_log.record_action", fake_record_action)

    pool = CandidatePool(
        candidates=(
            Candidate(
                evidence="e1",
                hypothesis="h1",
                sources=frozenset({CandidateSource.STRUCTURAL_ENTITY}),
            ),
            Candidate(
                evidence="e2", hypothesis="h1", sources=frozenset({CandidateSource.EMBEDDING_KNN})
            ),
        )
    )
    meta = {
        "h1": _meta("the hypothesis", box="box-h"),
        "e1": _meta("supporting evidence", tier=Tier.CASE),
        "e2": _meta("contradicting evidence", tier=Tier.CASE),
    }
    judge = EdgeJudge(
        _FakeLLM(
            {
                "supporting evidence": JudgedSign.SUPPORTS,
                "contradicting evidence": JudgedSign.REFUTES,
            }
        ),
        n_samples=3,
    )
    producer = _StubProducer(
        judge,
        candidates=_FakeCandidateAdapter(pool),
        node_meta=meta,
        credibility={"e1": 0.6, "e2": 0.6},
    )
    session = _FakeSession()

    result = await producer.produce(session)

    assert session.committed is True
    # Two edges written, one SUPPORTS one REFUTES, evidence -> hypothesis.
    assert {(w["src"], w["dst"], w["label"]) for w in writes} == {
        ("e1", "h1", "SUPPORTS"),
        ("e2", "h1", "REFUTES"),
    }
    # significance = uniform tier weight * credibility(0.6) = 0.6 for both.
    assert all(w["props"]["significance"] == pytest.approx(0.6) for w in writes)
    # One Action for the (single) judged hypothesis.
    assert len(actions) == 1 and len(result.action_ids) == 1
    assert {e.sign for e in result.edges} == {EdgeSign.SUPPORTS, EdgeSign.REFUTES}
    # Unanimous panels -> stable signs -> no finding.
    assert result.is_finding is False


@pytest.mark.asyncio
async def test_produce_drops_irrelevant_and_writes_nothing_for_empty_pool(monkeypatch) -> None:  # noqa: ANN001
    writes: list[dict] = []
    actions: list[dict] = []

    async def fake_merge_edge(session, *, src_id, dst_id, label, props):  # noqa: ANN001
        writes.append({"src": src_id, "dst": dst_id, "label": label, "props": props})

    async def fake_record_action(session, **kw):  # noqa: ANN001
        actions.append(kw)
        return uuid.uuid4()

    monkeypatch.setattr("iknos.db.age.merge_edge", fake_merge_edge)
    monkeypatch.setattr("iknos.provenance.action_log.record_action", fake_record_action)

    pool = CandidatePool(
        candidates=(
            Candidate(
                evidence="e1", hypothesis="h1", sources=frozenset({CandidateSource.EMBEDDING_KNN})
            ),
        )
    )
    meta = {"h1": _meta("hyp"), "e1": _meta("unrelated noise")}
    judge = EdgeJudge(_FakeLLM({"unrelated noise": JudgedSign.IRRELEVANT}), n_samples=3)
    producer = _StubProducer(
        judge, candidates=_FakeCandidateAdapter(pool), node_meta=meta, credibility={}
    )

    result = await producer.produce(_FakeSession())

    # The single candidate is judged irrelevant: no edge, no Action, but the drop is reported.
    assert writes == [] and actions == []
    assert result.edges == ()
    assert result.dropped == (("e1", "h1"),)
