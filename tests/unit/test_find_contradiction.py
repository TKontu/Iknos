"""Unit tests for the pure half of the §12 ``find-contradiction`` operator (G4.5).

DB-free, LLM-free: the claim-key clustering, atom assembly and sub-region →
:class:`~iknos.core.symbolic_gate.SymbolicQuery` build are exercised with hand-built rows, then run
through **real** :func:`~iknos.core.symbolic_gate.check_consistency` (the clingo engine is not
mocked — the point of the symbolic channel). What is pinned: a claim and its embedding-near negation
get the **same** claim key (so a ``P`` / ``¬P`` clash is detectable), a dissimilar claim gets a
different key, cross-model claims never co-cluster (G1.16), and the assembled query yields a genuine
CONTRADICTORY only on a real polarity twin (merely-contrary evidence abstains — the §7.2
conservatism). The operator's DB orchestration is covered by the integration test.
"""

from iknos.core.find_contradiction import (
    SubregionProposition,
    assemble_symbolic_query,
    assign_claim_keys,
)
from iknos.core.symbolic_gate import Consistency, check_consistency
from iknos.types.epistemic import Polarity

# Two near-identical unit vectors (cosine ~1.0 — a claim and its negation embed close, G1.14) and a
# dissimilar one (orthogonal-ish). bge-m3 is 1024-d but the engine is dimension-agnostic; 3-d toys
# suffice for the cosine clustering, exactly as the consistency.py unit tests use toy vectors.
_NEAR_A = (1.0, 0.0, 0.0)
_NEAR_A2 = (0.99, 0.01, 0.0)
_FAR = (0.0, 1.0, 0.0)
_MODEL = "bge-m3"


def _prop(node, prop, polarity, vector, *, model=_MODEL, text="claim"):  # noqa: ANN001, ANN202
    return SubregionProposition(
        node=node, proposition=prop, polarity=polarity, text=text, model=model, vector=vector
    )


# --- claim-key clustering ----------------------------------------------------------------------


def test_polarity_twin_shares_a_claim_key() -> None:
    # An asserted claim and its near-embedding negation cluster polarity-BLIND → the same key, so
    # their opposite Polarity makes them a P / ¬P pair the symbolic engine can clash.
    props = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A),
        _prop("r", "p2", Polarity.NEGATED, _NEAR_A2),
    ]
    keys = assign_claim_keys(props)
    assert keys["p1"] == keys["p2"]


def test_dissimilar_claims_get_distinct_keys() -> None:
    props = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A),
        _prop("r", "p2", Polarity.ASSERTED, _FAR),
    ]
    keys = assign_claim_keys(props)
    assert keys["p1"] != keys["p2"]


def test_keys_are_model_namespaced_so_cross_model_claims_never_clash() -> None:
    # Identical vectors but different embedding models → distinct keys (G1.16: cosine across models
    # is meaningless, so they can never be read as the same claim).
    props = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A, model="bge-m3"),
        _prop("r", "p2", Polarity.NEGATED, _NEAR_A, model="other-model"),
    ]
    keys = assign_claim_keys(props)
    assert keys["p1"] != keys["p2"]
    assert keys["p1"].startswith("bge-m3#") and keys["p2"].startswith("other-model#")


def test_clustering_is_order_independent() -> None:
    a = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A),
        _prop("r", "p2", Polarity.NEGATED, _NEAR_A2),
        _prop("s", "p3", Polarity.ASSERTED, _FAR),
    ]
    assert assign_claim_keys(a) == assign_claim_keys(list(reversed(a)))


# --- sub-region query assembly -----------------------------------------------------------------


def test_assembled_query_on_a_real_twin_is_contradictory() -> None:
    # h asserts a claim; the refuter negates the embedding-same claim → a real P ∧ ¬P.
    props = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A),
        _prop("r", "p2", Polarity.NEGATED, _NEAR_A2),
    ]
    q = assemble_symbolic_query(hypothesis_id="h", refuter_ids=["r"], supporter_ids=[], props=props)
    assert q is not None
    assert check_consistency(q).verdict is Consistency.CONTRADICTORY


def test_merely_contrary_refuter_is_unrelated_not_a_contradiction() -> None:
    # The refuter is a *different* claim (far embedding) — symbolic shares no atom → ABSTAIN, so the
    # gate holds the flip rather than auto-retracting. The §7.2 conservatism, structurally.
    props = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A),
        _prop("r", "p2", Polarity.ASSERTED, _FAR),
    ]
    q = assemble_symbolic_query(hypothesis_id="h", refuter_ids=["r"], supporter_ids=[], props=props)
    assert q is not None
    assert check_consistency(q).verdict is Consistency.UNRELATED


def test_query_is_none_when_hypothesis_has_no_embeddable_claim() -> None:
    # Only the refuter carries a proposition — the hypothesis cannot be placed in claim-space, so
    # the sub-region cannot be built (caller falls back to the ABSTAIN seam → flip held).
    props = [_prop("r", "p2", Polarity.NEGATED, _NEAR_A)]
    q = assemble_symbolic_query(hypothesis_id="h", refuter_ids=["r"], supporter_ids=[], props=props)
    assert q is None


def test_query_is_none_when_no_refuter_has_a_claim() -> None:
    props = [_prop("h", "p1", Polarity.ASSERTED, _NEAR_A)]
    q = assemble_symbolic_query(hypothesis_id="h", refuter_ids=["r"], supporter_ids=[], props=props)
    assert q is None


def test_supporter_claims_are_carried_as_context() -> None:
    # A supporter's claim lands in `context` (so a transitive clash *could* route through it); with
    # no twin it does not by itself create a contradiction.
    props = [
        _prop("h", "p1", Polarity.ASSERTED, _NEAR_A),
        _prop("r", "p2", Polarity.ASSERTED, _FAR),  # unrelated refuter
        _prop("s", "p3", Polarity.ASSERTED, _FAR),  # supporter, same far cluster
    ]
    q = assemble_symbolic_query(
        hypothesis_id="h", refuter_ids=["r"], supporter_ids=["s"], props=props
    )
    assert q is not None
    # The supporter's claim key appears in context (one atom, the far cluster).
    assert len(q.context) == 1
