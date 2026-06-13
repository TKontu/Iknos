"""Candidate-generation recall scoring for Trial A1 (architecture.md §5.1; ``docs/todo_trials.md``).

Trial A1 asks the binding question for the §5.1 candidate funnel: at a given candidate **budget**
(how many ``(evidence → hypothesis)`` pairs are sent to the expensive LLM adjudication stage),
what fraction of the **planted** edges does the funnel recall — measured **separately for
supporters and refuters**, because *a missed refuter is a silent false negative* (the dangerous
kind) and refuter recall is the decision's binding constraint. The further-binding subset is the
**dissimilar refuter** — a refuting fact semantically far from the hypothesis it attacks, which
the embedding k-NN stage under-generates and the structural prior is meant to rescue (§5.1).

This module is the **pure scorer** that composes :func:`iknos.trials.metrics.recall_at_budget`
into the A1 measurement: gold planted edges in, split-by-sign recall + adjudication cost out. It
is deliberately **space-agnostic** — it scores any sequence of retrieved ``(evidence, hypothesis)``
pairs against gold pairs in the *same* id space, so the live A1 runner
(``scripts/a1_refuter_recall.py``) can derive gold from the V1 planted manifest (no V2 labels)
and project a real :class:`~iknos.core.candidates.CandidatePool` into that space, while the scorer
stays pure, stdlib-only, LLM-free and unit-testable on hand-built fixtures. The funnel itself
(``core/candidates.py``) is **not** imported here — that keeps this inside the V3 trials import
boundary; the runner does the (DB-bound) generation and projection.

**Recall, not precision (§5.1).** The funnel tunes for recall; a spurious candidate is cheaply
rejected at adjudication, a missed one is gone forever. So A1 reports *recall at a cost*, never an
F-score that would trade a recalled refuter for fewer candidates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from iknos.trials.metrics import recall_at_budget

# A retrieved candidate, or a gold edge endpoint, as an ``(evidence_id, hypothesis_id)`` pair. The
# id space is the caller's (planted ids for the manifest gold, AGE node ids for a live pool); the
# scorer only requires the retrieved pairs and the gold pairs share one space.
type EdgePair = tuple[str, str]


class EdgeSign(StrEnum):
    """The categorical sign of a planted evidential edge (§5/§8 — sign before magnitude).

    A1 splits recall on this: supporter recall is the easy axis (RAG finds support), refuter
    recall is the binding constraint (the §5.1 dissimilar-refuter risk).
    """

    SUPPORTS = "supports"
    REFUTES = "refutes"


@dataclass(frozen=True)
class GoldEdge:
    """One planted ``evidence → hypothesis`` edge the funnel should recall.

    ``evidence`` and ``hypothesis`` are ids in whatever space the caller scores in (the V1 planted
    item id and the hypothesis id, for the manifest-derived gold). ``sign`` splits supporter vs
    refuter recall; ``dissimilar`` marks the §5.1 binding subset — a refuter semantically far from
    its target (the ``dissimilar_refuter`` planted kind). ``dissimilar`` is only meaningful on a
    ``REFUTES`` edge (a "dissimilar supporter" is not a tracked category).
    """

    evidence: str
    hypothesis: str
    sign: EdgeSign
    dissimilar: bool = False

    @property
    def pair(self) -> EdgePair:
        return (self.evidence, self.hypothesis)


@dataclass(frozen=True)
class RecallResult:
    """Split candidate recall at one budget, with the adjudication cost it was bought at.

    ``budget`` is the candidate budget scored at (how many ranked candidates count);
    ``n_candidates`` is the distinct candidates within that budget — the **adjudication cost**
    A1 trades recall against. The three recalls are ``None`` when their gold subset is empty
    (recall of nothing is undefined — never reported as a misleading ``0.0`` or ``1.0``, mirroring
    :mod:`iknos.trials.metrics`). ``dissimilar_refuter_recall`` is the binding constraint.
    """

    budget: int
    n_candidates: int
    supporter_recall: float | None
    refuter_recall: float | None
    dissimilar_refuter_recall: float | None
    n_supporters: int
    n_refuters: int
    n_dissimilar_refuters: int


def project_to_gold(
    node_pairs: Sequence[EdgePair],
    evidence_map: Mapping[str, str],
    hypothesis_map: Mapping[str, str],
) -> list[EdgePair]:
    """Project candidate pairs from the funnel's id space (AGE node ids) into the gold id space.

    The §5.1 funnel emits ``(evidence_node, hypothesis_node)`` pairs keyed by AGE node ids; the
    gold edges are keyed by planted-item / hypothesis ids. A pair survives iff **both** endpoints
    map — its evidence node to a planted-evidence id (``evidence_map``) **and** its hypothesis node
    to a gate-hypothesis id (``hypothesis_map``); a pair touching a node that is not a planted
    edge endpoint is dropped (it is a candidate, but not one of the *planted* edges A1 scores
    recall of). Order is preserved, so a ranked pool stays ranked for budgeted recall.

    The two maps are what the live runner builds from the ingest (planted anchor → reasoning-node
    id, hypothesis label → ``Hypothesis`` node id); kept as inputs so this projection stays pure
    and unit-testable without a DB.
    """
    projected: list[EdgePair] = []
    for ev_node, hyp_node in node_pairs:
        evidence = evidence_map.get(ev_node)
        hypothesis = hypothesis_map.get(hyp_node)
        if evidence is not None and hypothesis is not None:
            projected.append((evidence, hypothesis))
    return projected


def _subset_recall(
    retrieved: Sequence[EdgePair], gold_pairs: set[EdgePair], budget: int
) -> float | None:
    """Recall of one gold subset at ``budget`` — ``None`` if the subset is empty (undefined)."""
    if not gold_pairs:
        return None
    return recall_at_budget(retrieved, gold_pairs, budget)


def score_recall(
    retrieved: Sequence[EdgePair],
    gold: Sequence[GoldEdge],
    *,
    budget: int | None = None,
) -> RecallResult:
    """Score split candidate recall of ``gold`` by the ``retrieved`` candidate list at ``budget``.

    ``retrieved`` is the candidate list **in rank order** if a ranking exists (e.g. embedding
    cosine), or any order for the unscored union funnel — in which case pass ``budget=None`` (the
    default, ``budget = len(retrieved)``) to score *set* recall of the whole pool and read
    ``n_candidates`` as its size. Vary ``budget`` (or the funnel's ``k``) to trace the
    recall-vs-cost curve A1's decision reads.

    Recall is computed per gold subset via :func:`iknos.trials.metrics.recall_at_budget` (which
    de-duplicates within the budget window, so a repeated candidate cannot inflate recall):
    supporters, refuters, and the dissimilar-refuter subset. Raises ``ValueError`` only for a
    negative budget (propagated from the metric); an empty subset yields ``None``, not an error.
    """
    if budget is None:
        budget = len(retrieved)

    supporter_pairs = {e.pair for e in gold if e.sign is EdgeSign.SUPPORTS}
    refuter_pairs = {e.pair for e in gold if e.sign is EdgeSign.REFUTES}
    dissimilar_pairs = {e.pair for e in gold if e.sign is EdgeSign.REFUTES and e.dissimilar}

    return RecallResult(
        budget=budget,
        n_candidates=len(set(retrieved[:budget])),
        supporter_recall=_subset_recall(retrieved, supporter_pairs, budget),
        refuter_recall=_subset_recall(retrieved, refuter_pairs, budget),
        dissimilar_refuter_recall=_subset_recall(retrieved, dissimilar_pairs, budget),
        n_supporters=len(supporter_pairs),
        n_refuters=len(refuter_pairs),
        n_dissimilar_refuters=len(dissimilar_pairs),
    )


def recall_curve(
    retrieved: Sequence[EdgePair],
    gold: Sequence[GoldEdge],
    budgets: Sequence[int],
) -> list[RecallResult]:
    """:func:`score_recall` at each budget — the recall-vs-cost curve A1's decision reads.

    A convenience over a ranked ``retrieved`` list: as the budget grows, recall rises and cost
    rises with it; the decision picks the smallest budget recalling the target fraction of
    **refuters** (the binding constraint). For the unscored union funnel, sweep the funnel's ``k``
    instead and call :func:`score_recall` once per generated pool.
    """
    return [score_recall(retrieved, gold, budget=b) for b in budgets]
