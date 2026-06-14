"""A1 — candidate-generation refuter-recall harness (Trial A1; architecture.md §5.1).

Trial A1 measures the §5.1 candidate funnel's recall of the **planted** ``evidence → hypothesis``
edges at a candidate budget, **split supporter vs refuter** — refuter recall (and its
dissimilar-refuter subset) is the binding constraint, because a missed refuter is a silent false
negative. This module is the **measurement harness** wired against the *existing* funnel
(``core/candidates.py``) and the pure A1 scorer (``iknos.trials.a1_recall``).

**Scope of this scaffolding (LLM-free, label-free, DB-free).** The committed, runnable parts are:

1. **Gold inventory from the V1 planted manifest** (``build_gold_edges``) — the planted
   ``supports`` / ``refutes`` cross-references in ``tests/fixtures/gate_corpus/manifest.toml`` are
   the planted edge ground truth, derivable with **no V2 labels** (they are the answer key the
   manifest already encodes). This is real.
2. **The harness path** funnel → :func:`iknos.trials.a1_recall.project_to_gold` →
   :func:`iknos.trials.a1_recall.score_recall`, exercised end-to-end on a **synthetic** scenario
   over the *real* funnel (``structural_entity_candidates`` / ``embedding_knn_candidates`` /
   ``funnel``) with no DB. The synthetic embeddings are illustrative — they demonstrate the
   harness discriminates the union funnel (which recovers the dissimilar refuter via the
   structural prior) from an embedding-only funnel (which misses it); **they are not the gate
   measurement** and no recall number here describes the corpus.

**Deferred to the live run (needs a DB + the embedding/LLM services up — not started here).** The
actual A1 numbers require the gate corpus *ingested* into an isolated pgvector/AGE database so the
funnel has real proposition embeddings and ``INVOLVES`` edges, plus the planted-anchor →
reasoning-node-id and hypothesis-label → ``Hypothesis``-node-id maps the ingest produces. The
recipe is in the generated report (``docs/trials/a1_refuter_recall.md``); per host policy this
script never starts containers and never claims an unmeasured number.

Usage::

    uv run python -m scripts.a1_refuter_recall --out docs/trials/a1_refuter_recall.md
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from iknos.core.candidates import (
    CandidatePool,
    EmbeddedNode,
    InvolvesRow,
    embedding_knn_candidates,
    funnel,
    structural_entity_candidates,
)
from iknos.trials.a1_recall import (
    EdgeSign,
    GoldEdge,
    RecallResult,
    project_to_gold,
    score_recall,
)
from iknos.trials.report import comparison_table
from tests.fixtures.gate_corpus import GateCorpus, PlantedKind, load_gate_corpus

# Target refuter recall the A1 decision is read against (§5.1: pick the smallest budget recalling
# ≥ this fraction of planted refuters). Recorded here as the decision threshold; the live run
# fills in the achieved value.
REFUTER_RECALL_TARGET = 0.9


# ─────────────────────────────────────────────────────────────────────────────────────────
# Gold inventory — the planted edges, derived from the V1 manifest (no V2 labels).
# ─────────────────────────────────────────────────────────────────────────────────────────


def build_gold_edges(gate: GateCorpus) -> list[GoldEdge]:
    """The planted ``evidence → hypothesis`` edges, from the manifest ``supports``/``refutes``.

    A planted item's ``supports`` cross-reference is a planted **supporter** edge (evidence →
    hypothesis); its ``refutes`` cross-reference a planted **refuter** edge. The ``dissimilar``
    flag is set when the refuter's kind is :attr:`PlantedKind.DISSIMILAR_REFUTER` — the §5.1
    binding subset (a refuter semantically far from its target). No V2 gold labels are consulted;
    the manifest's planted inventory *is* the ground truth A1 scores candidate recall against.
    """
    edges: list[GoldEdge] = []
    for item in gate.planted:
        if item.supports is not None:
            edges.append(
                GoldEdge(evidence=item.id, hypothesis=item.supports, sign=EdgeSign.SUPPORTS)
            )
        if item.refutes is not None:
            edges.append(
                GoldEdge(
                    evidence=item.id,
                    hypothesis=item.refutes,
                    sign=EdgeSign.REFUTES,
                    dissimilar=item.kind is PlantedKind.DISSIMILAR_REFUTER,
                )
            )
    return edges


def pool_to_node_pairs(pool: CandidatePool) -> list[tuple[str, str]]:
    """A :class:`CandidatePool` as ordered ``(evidence_node, hypothesis_node)`` string pairs.

    The funnel's deterministic pair order is preserved; the live run replaces it with the
    embedding-rank order when budgeting a ranked pool. Stringifies the ``NodeId``s for the
    space-agnostic scorer.
    """
    return [(str(c.evidence), str(c.hypothesis)) for c in pool.candidates]


# ─────────────────────────────────────────────────────────────────────────────────────────
# Synthetic wiring demonstration — the REAL funnel on synthetic, DB-free inputs.
#
# Illustrative only: the embeddings are hand-built to make the §5.1 dynamic legible (the
# dissimilar refuter is embedding-far from its hypothesis but structurally linked to it). It
# proves the harness path runs and discriminates union vs embedding-only — NOT the gate numbers.
# ─────────────────────────────────────────────────────────────────────────────────────────

_MODEL = "demo-model"
_DEMO_K = (
    3  # small k + distractors so the embedding stage ranks the dissimilar refuter out of top-k
)


def _unit(*components: float) -> tuple[float, ...]:
    norm = math.sqrt(sum(c * c for c in components))
    return tuple(c / norm for c in components) if norm else tuple(components)


@dataclass(frozen=True)
class DemoScenario:
    hyp_nodes: frozenset[str]
    ev_nodes: frozenset[str]
    hyp_embedded: tuple[EmbeddedNode, ...]
    ev_embedded: tuple[EmbeddedNode, ...]
    involves: tuple[InvolvesRow, ...]
    evidence_map: dict[str, str]  # evidence node id -> planted gold id
    hypothesis_map: dict[str, str]  # hypothesis node id -> gold hypothesis id


def build_demo_scenario(gold: Sequence[GoldEdge]) -> DemoScenario:
    """A synthetic active subgraph that reproduces the §5.1 dissimilar-refuter geometry.

    For every gold hypothesis a one-hot direction ``e_H`` is chosen. A **supporter** evidence node
    is embedded *on* its hypothesis direction (cosine 1 — the embedding stage finds it); a
    **dissimilar refuter** is embedded **orthogonal** to its hypothesis (cosine 0 — the embedding
    stage misses it) but given a shared ``INVOLVES`` entity with that hypothesis (the structural
    prior catches it). A handful of mildly-similar **distractor** evidence nodes per hypothesis
    push the orthogonal refuter out of the top-``k`` so an embedding-only funnel genuinely drops it.
    Node ids are distinct from gold ids, with explicit maps, so the projection step is exercised.
    """
    hyp_ids = sorted({e.hypothesis for e in gold})
    refuters = [e for e in gold if e.sign is EdgeSign.REFUTES]
    # Axes: one per hypothesis, one shared distractor-blend axis, one dedicated axis per refuter.
    # A refuter's own axis is orthogonal to *every* hypothesis (cosine 0 with all of them), so it
    # is genuinely embedding-invisible and never crowds a supporter out of a hypothesis's top-k.
    axis = {h: i for i, h in enumerate(hyp_ids)}
    blend_axis = len(hyp_ids)
    refuter_axis = {e.evidence: blend_axis + 1 + i for i, e in enumerate(refuters)}
    dim = blend_axis + 1 + len(refuters)

    def onehot(index: int) -> tuple[float, ...]:
        return _unit(*(1.0 if i == index else 0.0 for i in range(dim)))

    def blended(h: str) -> tuple[float, ...]:  # cosine ~0.9 with e_H (a near-miss distractor)
        return _unit(
            *(0.9 if i == axis[h] else (0.436 if i == blend_axis else 0.0) for i in range(dim))
        )

    hyp_embedded: list[EmbeddedNode] = []
    hypothesis_map: dict[str, str] = {}
    for h in hyp_ids:
        node = f"hyp::{h}"
        hypothesis_map[node] = h
        hyp_embedded.append(EmbeddedNode(node=node, model=_MODEL, vector=onehot(axis[h])))

    ev_embedded: list[EmbeddedNode] = []
    involves: list[InvolvesRow] = []
    evidence_map: dict[str, str] = {}

    # Distractor evidence per hypothesis (unmapped — not planted edges; they fill the top-k).
    for h in hyp_ids:
        for j in range(_DEMO_K):
            node = f"ev::distractor::{h}::{j}"
            ev_embedded.append(EmbeddedNode(node=node, model=_MODEL, vector=blended(h)))

    # The planted evidence nodes (mapped to their gold id).
    for edge in gold:
        node = f"ev::{edge.evidence}"
        evidence_map[node] = edge.evidence
        if edge.sign is EdgeSign.SUPPORTS:
            vector = onehot(
                axis[edge.hypothesis]
            )  # on its hypothesis direction: embedding finds it
        else:  # a (dissimilar) refuter: its own axis (cosine 0 with every hypothesis), but linked
            vector = onehot(refuter_axis[edge.evidence])
            entity = f"ent::{edge.hypothesis}"
            involves.append(InvolvesRow(node=node, entity=entity))
            involves.append(InvolvesRow(node=f"hyp::{edge.hypothesis}", entity=entity))
        ev_embedded.append(EmbeddedNode(node=node, model=_MODEL, vector=vector))

    return DemoScenario(
        hyp_nodes=frozenset(hypothesis_map),
        ev_nodes=frozenset(evidence_map)
        | {e.node for e in ev_embedded if e.node not in evidence_map},
        hyp_embedded=tuple(hyp_embedded),
        ev_embedded=tuple(ev_embedded),
        involves=tuple(involves),
        evidence_map=evidence_map,
        hypothesis_map=hypothesis_map,
    )


def _embedding_pool(scn: DemoScenario) -> CandidatePool:
    embedding = embedding_knn_candidates(
        hypotheses=scn.hyp_embedded, evidence=scn.ev_embedded, k=_DEMO_K
    )
    return funnel(embedding)


def _union_pool(scn: DemoScenario) -> CandidatePool:
    structural = structural_entity_candidates(
        hypotheses=scn.hyp_nodes, evidence=scn.ev_nodes, involves=scn.involves
    )
    embedding = embedding_knn_candidates(
        hypotheses=scn.hyp_embedded, evidence=scn.ev_embedded, k=_DEMO_K
    )
    return funnel(structural, embedding)


def _score_pool(pool: CandidatePool, scn: DemoScenario, gold: Sequence[GoldEdge]) -> RecallResult:
    projected = project_to_gold(pool_to_node_pairs(pool), scn.evidence_map, scn.hypothesis_map)
    return score_recall(projected, gold)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Report rendering.
# ─────────────────────────────────────────────────────────────────────────────────────────


def _fmt_recall(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _gold_table(gold: Sequence[GoldEdge]) -> str:
    rows: dict[str, dict[str, str | float]] = {}
    for e in gold:
        rows[f"{e.evidence} → {e.hypothesis}"] = {
            "sign": e.sign.value,
            "dissimilar": "yes" if e.dissimilar else "no",
        }
    return comparison_table(rows, row_header="planted edge")


def _recall_row(label: str, pool: CandidatePool, r: RecallResult) -> dict[str, str | float]:
    return {
        "candidates": len(pool),
        "supporter recall": _fmt_recall(r.supporter_recall),
        "refuter recall": _fmt_recall(r.refuter_recall),
        "dissimilar-refuter recall": _fmt_recall(r.dissimilar_refuter_recall),
    }


def build_report(gate: GateCorpus, gold: Sequence[GoldEdge]) -> str:
    scn = build_demo_scenario(gold)
    emb_pool = _embedding_pool(scn)
    uni_pool = _union_pool(scn)
    emb_r = _score_pool(emb_pool, scn, gold)
    uni_r = _score_pool(uni_pool, scn, gold)

    n_sup = sum(1 for e in gold if e.sign is EdgeSign.SUPPORTS)
    n_ref = sum(1 for e in gold if e.sign is EdgeSign.REFUTES)
    n_dis = sum(1 for e in gold if e.sign is EdgeSign.REFUTES and e.dissimilar)

    lines: list[str] = []
    a = lines.append

    a("# Trial A1 — Candidate-generation refuter-recall harness (scaffolding)")
    a("")
    a(
        "**Instrument A / Trial A1** (`docs/todo_trials.md`) — *⚠ may force redesign*. Gates "
        "Phase-4 candidate generation and Phase-6 generate-candidates. **This is the LLM-free, "
        "label-free, "
        "DB-free harness scaffolding; the live measurement is pending (needs the gate corpus "
        "ingested into a DB + the embedding/LLM services up — §4).** No recall number below "
        "describes the corpus."
    )
    a("")
    a(
        "- **Harness:** `scripts/a1_refuter_recall.py` (reproduce: "
        "`uv run python -m scripts.a1_refuter_recall`); scorer `iknos.trials.a1_recall` "
        "(pure, unit-tested); funnel under test `core/candidates.py` (read-only)."
    )
    a(
        f"- **Decision threshold (recorded):** adopt the smallest candidate budget recalling "
        f"≥ {REFUTER_RECALL_TARGET:.0%} of planted **refuters** (§5.1 binding constraint); the "
        f"live run fills in the achieved value."
    )
    a("")

    a("## 1. Gold inventory (real — from the V1 planted manifest, no V2 labels)")
    a("")
    a(
        f"The planted `supports`/`refutes` cross-references in "
        f"`tests/fixtures/gate_corpus/manifest.toml` are the planted edge ground truth A1 scores "
        f"recall against. **{n_sup} supporter**, **{n_ref} refuter** ({n_dis} dissimilar) planted "
        f"edges."
    )
    a("")
    a(_gold_table(gold))
    a("")
    a(
        "The dissimilar refuters are the §5.1 binding subset: embedding k-NN under-generates them "
        "(a refuter can be semantically far from its target), so the structural prior is the "
        "recall floor that must catch them — exactly what the live measurement tests."
    )
    a("")

    a("## 2. Harness path")
    a("")
    a(
        "`build_gold_edges(manifest)` → run the funnel (`core/candidates.py`) over the active "
        "subgraph → `pool_to_node_pairs` → `a1_recall.project_to_gold` (node-id space → planted-id "
        "space, via the ingest's anchor→node and hypothesis→node maps) → `a1_recall.score_recall` "
        "(supporter / refuter / dissimilar-refuter recall, split). Budget is applied in **node "
        "space** (the funnel's `k` / rank order is the cost knob); the projected gold-space recall "
        "is read at each budget. Cost is the node-space pool size, not the projected count."
    )
    a("")

    a("## 3. Synthetic wiring demonstration (illustrative — NOT the gate measurement)")
    a("")
    a(
        "The real funnel run on a **synthetic** active subgraph whose geometry reproduces the §5.1 "
        "case: each supporter is embedding-near its hypothesis; each dissimilar refuter is "
        "embedding-**orthogonal** to its hypothesis but shares an `INVOLVES` entity with it; "
        "distractors fill the top-k. The embeddings are hand-built, so these numbers reflect the "
        "*synthetic* similarity, **not** the corpus — they show only that the harness runs against "
        "the real funnel and discriminates the two funnel strategies."
    )
    a("")
    demo_rows: dict[str, dict[str, str | float]] = {
        "embedding-knn only": _recall_row("emb", emb_pool, emb_r),
        "union (structural ∪ embedding)": _recall_row("uni", uni_pool, uni_r),
    }
    a(comparison_table(demo_rows, row_header="funnel"))
    a("")
    a(
        f"As designed: the embedding-only funnel recalls supporters "
        f"({_fmt_recall(emb_r.supporter_recall)}) but **misses the dissimilar refuters** "
        f"({_fmt_recall(emb_r.dissimilar_refuter_recall)}); the recall-first **union** recovers "
        f"them via the structural prior ({_fmt_recall(uni_r.dissimilar_refuter_recall)}) — the "
        f"§5.1 mitigation the live A1 run quantifies on real embeddings. The harness measures it."
    )
    a("")

    a("## 4. Live-run recipe (deferred — needs a DB + embedding/LLM services up)")
    a("")
    a(
        "Not run here (host policy: no containers started without approval; vLLM is down). To "
        "produce the real A1 numbers when the services are up:"
    )
    a("")
    a(
        "1. Ingest the gate corpus (d01–d10) into an **isolated** ephemeral database "
        "(`CREATE DATABASE` per the C3 pattern) — perception + extraction, so the funnel has real "
        "`proposition_embeddings` and `INVOLVES` edges. *(Extraction needs vLLM **and** R11-H "
        "merged — see the R11-H gate.)*"
    )
    a(
        "2. Build the two projection maps from the ingest: planted-anchor → reasoning-node id "
        "(locate each planted quote's span → its `EVIDENCED_BY` reasoning node) and hypothesis "
        "label → `Hypothesis` node id."
    )
    a(
        "3. Sweep the funnel knobs (embedding `k`, `FunnelStrategy`, `min_similarity`) via "
        "`CandidateGenerationAdapter.generate`; for each, `project_to_gold` + `score_recall`."
    )
    a(
        "4. Record the recall-vs-cost curve; the decision is the smallest budget reaching the "
        f"{REFUTER_RECALL_TARGET:.0%} refuter-recall target. **Redesign trigger (§5.1):** if "
        "similarity + entity/topic generation still cannot recall the dissimilar refuters, "
        "contradiction-finding must become a dedicated pass over the hypothesis neighbourhood, not "
        "a funnel-gated step."
    )
    a("")

    a("## 5. Status & gating")
    a("")
    a(
        "- **Scaffolding complete, run pending.** Gold inventory, scorer, projection and the "
        "funnel-wired harness are committed and unit-tested; the measurement awaits a live DB + "
        "embedding service (and, for the extraction half, vLLM + R11-H)."
    )
    a(
        "- **No labels required** — gold is the V1 planted manifest; this does not touch the V2 "
        "label families (which do not exist yet)."
    )
    a(
        "- **A1 is ⚠ may-force-redesign** — the dissimilar-refuter recall is the result that can "
        "trigger the §5.1 redesign; do not harden Phase-4 candidate generation on it until the "
        "live run lands."
    )
    a("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=None, help="write the markdown report here (also to stdout)"
    )
    args = parser.parse_args()

    gate = load_gate_corpus()
    gold = build_gold_edges(gate)
    report = build_report(gate, gold)

    print(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
