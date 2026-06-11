"""Phase 4 candidate generation (G4.2; architecture §5.1).

Edge adjudication (the §8 sign+strength LLM judgment, G4.3) is expensive, so it must run only
on pairs worth assessing — all-pairs is ``O(n²)`` LLM calls, intractable at scale, and most
pairs are unrelated. **Candidate generation (cheap, high-recall, approximate) and edge
adjudication (expensive, high-precision, LLM) are two separate stages** (§5.1); this module is
the first. It is the standard *blocking / candidate-generation* funnel from entity resolution
and link prediction: spend compute in inverse proportion to how many pairs survive each cheap
stage, and let the LLM stage do precision.

A *candidate* is an ordered ``(evidence → hypothesis)`` pair — the schema direction a
``SUPPORTS``/``REFUTES`` edge would run (§5, §10), so generation already knows which node is the
bearer and which is the target. ``sign`` is **not** decided here (that is the §8 "sign before
magnitude" judgment, G4.3); generation only proposes *which pairs to look at*.

**The funnel, cheap → expensive (§5.1).** Stage 4 (LLM adjudication) is G4.3; slice 1 shipped the
recall-first funnel core and **stage 1, the structural-entity prior** (the refuter-safe recall
floor), slice 2 adds **stage 2, the embedding nearest-neighbour workhorse**, with the remaining
cheap generators as documented seams:

1. **Structural priors** — near-free. Two reasoning nodes sharing an ``Actor``/``Object`` (via an
   ``INVOLVES`` edge), restricted to the active box scope, are candidates. *(Shipped, slice 1:
   :func:`structural_entity_candidates`. The sparse/keyword co-occurrence prior is a further
   :class:`CandidateSource` that unions in at the same seam.)*
2. **Embedding nearest-neighbour** — each reasoning node's k nearest claims (by cosine over the
   ``proposition_embeddings`` dense index it is ``EVIDENCED_BY``, §4) are relatedness candidates;
   the workhorse stage. *(Shipped, slice 2: :func:`embedding_knn_candidates` + the
   :class:`CandidateGenerationAdapter` cross-store read. The k-NN math is **exact** in-memory over
   the active working set — the recall ceiling the validation gate G4.6 measures any approximate
   ANN index against; the pgvector ``<=>`` ivfflat/hnsw push-down is the performance seam for when
   the active set outgrows in-memory search, unioning in without a contract change.)*
3. **Coarse-to-fine** — reuse the §2 multi-level chunk hierarchy as a pruning tree: match coarse,
   descend to proposition pairing only within survivors. *(Deferred seam: needs the
   ``partOf`` level derivation.)*

**Tune for recall early, precision late (§5.1).** A missed candidate is an edge never considered
— a silent false negative, the dangerous kind; a spurious candidate is just cheaply rejected at
adjudication. So the cheap stages favour recall: this layer does **not** score or rank candidates
(precision is the LLM stage's job), and the funnel **unions** generators rather than intersecting
them (the :data:`DEFAULT_STRATEGY` decision below).

**The dissimilar-refuter problem (§5.1) — why the structural prior ships first and why the funnel
unions.** A *refuting* fact can be semantically dissimilar to the hypothesis it attacks, so
embedding-NN under-generates refutation candidates and would bias the system toward finding
support and missing contradiction — a serious flaw given how central refutation is. The structural
prior pulls candidates by a hypothesis's *constituent entities*, not its embedding, so it catches
the dissimilar refuter the embedding stage misses; **intersecting** the two stages would drop it
again. Hence the recall-first union default. (``find-contradiction`` as a first-class refuter
*generator* is G4.5; this is the structural half of the mitigation.)

Pure/DB split mirrors ``core/qbaf_adapter.py``: the value types and the funnel/stage logic are
DB-free (unit-testable with hand-built rows); :class:`CandidateGenerationAdapter` does the AGE
reads in its ``async`` methods behind a lazy ``iknos.db.age`` import, reusing the shared
``load_active_box_ids`` / ``load_reasoning_nodes`` / ``load_hypothesis_ids`` reads so the
active-subgraph definition cannot diverge from the propagation/adjudication loads.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from iknos.core.derivation_adapter import (
    load_active_box_ids,
    load_hypothesis_ids,
    load_reasoning_nodes,
)
from iknos.core.truth_maintenance import NodeId


class CandidateSource(StrEnum):
    """Which funnel generator proposed a pair (§5.1) — provenance, not a score.

    A pair may be proposed by several generators; the funnel unions their sources onto one
    :class:`Candidate` (a pair found by *both* the structural and embedding stages is one
    candidate carrying both sources, not two). Adding a member is additive — a new cheap
    generator unions in at the funnel seam without changing the contract.
    """

    STRUCTURAL_ENTITY = "structural-entity"  # shared INVOLVES Actor/Object (shipped — stage 1)
    EMBEDDING_KNN = "embedding-knn"  # proposition-embedding nearest-neighbour (shipped — stage 2)
    # Deferred cheap generator (seam; see module docstring):
    # STRUCTURAL_KEYWORD = "structural-keyword"  # sparse/keyword co-occurrence (stage 1)


class FunnelStrategy(StrEnum):
    """How :func:`funnel` combines the cheap generators' outputs (§5.1).

    The decision parallels the Layer-B (Gödel over Viterbi), QBAF (DF-QuAD over Quadratic Energy)
    and fusion (averaging over cumulative) choices — *default to the operator that cannot lose a
    true candidate; retain the other at the seam* — decided here with the
    :func:`~tests.unit.test_candidates` dissimilar-refuter fixture:

    - **``UNION``** (the recall-first default) — a pair any generator proposes survives, sources
      merged. The dissimilar refuter (structurally related, embedding-dissimilar) is caught by
      the structural stage and **kept**; this is the §5.1 "recall early" discipline and the
      structural half of the dissimilar-refuter mitigation.
    - **``INTERSECT``** — keep only pairs every generator proposed. This **drops the dissimilar
      refuter** (the embedding stage never proposed it), re-introducing exactly the support-bias
      §5.1 forbids — so it is *never* the candidate-generation default. Retained at the seam as a
      precision pre-filter for a recall-saturated sub-domain only.
    """

    UNION = "union"
    INTERSECT = "intersect"


DEFAULT_STRATEGY = FunnelStrategy.UNION
"""Recall-first union (§5.1) — never lose a candidate a cheap stage found."""


DEFAULT_K = 10
"""How many nearest claims the embedding stage proposes per node (§5.1).

A recall/precision tunable, not an epistemic either/or — wider ``k`` raises recall at the cheap
stage and leaves more for the §8 LLM stage to reject; the validation gate (G4.6) calibrates it
against measured candidate/refuter recall. Defaulted generously (precision is the LLM's job)."""


DEFAULT_MIN_SIMILARITY: float | None = None
"""The embedding stage's recall-first decision (G4.2's UNION-style fixture), recorded eyes-open.

Whether to apply a **cosine-similarity floor** to the k-NN is the embedding analogue of the
funnel's UNION-vs-INTERSECT choice, and it is the **same dissimilar-refuter throughline** (§5.1):

- **``None`` (the default) — pure rank-based top-k, no floor.** A refuting claim can be
  semantically *dissimilar* to the hypothesis it attacks, so it sits at a low cosine yet still
  inside the top-``k``; a floor would **drop exactly that refuter**, re-introducing the
  support-bias §5.1 forbids. *Default to the operator that cannot lose a true candidate.*
- **A floor in ``[0, 1]``** — keep only neighbours at least that similar: a precision pre-filter
  for a recall-saturated sub-domain. Retained at the seam (the ``min_similarity`` parameter of
  :func:`embedding_knn_candidates`), never the candidate-generation default.

Parallels the Layer-B (Gödel), QBAF (DF-QuAD), fusion (averaging) and funnel (UNION) choices —
decided with the :func:`~tests.unit.test_candidates` dissimilar-refuter k-NN fixture. Reversible —
a value, not a branch."""


@dataclass(frozen=True)
class Candidate:
    """One ``(evidence → hypothesis)`` pair worth adjudicating (§5.1, §5).

    ``evidence`` is the bearer (a ``Fact``/``Conclusion``), ``hypothesis`` the target it bears on
    — the schema direction a ``SUPPORTS``/``REFUTES`` edge would run (§5, §10). The pair is
    **unscored**: candidate generation tunes for recall, leaving precision (and ``sign``) to the
    §8 LLM judgment (G4.3). ``sources`` records which generators proposed it; ``shared_entities``
    is the structural prior's rationale (the ``Actor``/``Object`` ids linking the pair, empty for
    a source that supplies none) — provenance the §8 *relative* judgment can rank within.
    """

    evidence: NodeId
    hypothesis: NodeId
    sources: frozenset[CandidateSource]
    shared_entities: frozenset[NodeId] = field(default_factory=frozenset)

    @property
    def key(self) -> tuple[NodeId, NodeId]:
        """The pair identity the funnel dedups on — direction included (§5)."""
        return (self.evidence, self.hypothesis)


@dataclass(frozen=True)
class CandidatePool:
    """The deduped set of candidates surviving the funnel — the input to §8 adjudication (G4.3).

    ``candidates`` is deterministically ordered (by pair) so a replay/trace is stable regardless
    of generator/row iteration order (§10). One :class:`Candidate` per ``(evidence, hypothesis)``
    pair — generators that proposed the same pair are merged, their sources/entities unioned.
    """

    candidates: tuple[Candidate, ...] = ()

    def __len__(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True)
class InvolvesRow:
    """One active ``INVOLVES`` edge as read from AGE: a reasoning node references an entity (§10).

    ``node`` is a ``Fact``/``Conclusion``/``Hypothesis`` id, ``entity`` an ``Actor``/``Object`` id,
    ``role`` the entity's role in the claim (``subject``/``object``/``instrument``; the privileged
    ``subject`` anchors derived abstraction level, §14 — carried for the coarse-to-fine seam,
    unused by the role-agnostic structural prior).
    """

    node: NodeId
    entity: NodeId
    role: str | None = None


@dataclass(frozen=True)
class EmbeddedNode:
    """One reasoning node's dense vector for the embedding stage — its claim's proposition vector.

    A reasoning node (``Fact``/``Conclusion``/``Hypothesis``) is ``EVIDENCED_BY`` one or more
    ``Proposition``s (§4, §10), each embedded in ``proposition_embeddings``; this is one such
    ``(node, model, vector)`` triple. A node with several propositions yields several
    :class:`EmbeddedNode`s sharing ``node`` — the k-NN represents the node by its **best-matching**
    proposition (recall-first; a node is a candidate if *any* of its claims is near the target).

    ``model`` is the embedding-model id (``proposition_embeddings.model``) — the **vector-space
    identity** (G1.16): cosine across two models is meaningless, so :func:`embedding_knn_candidates`
    only ever compares two vectors with the *same* ``model``. ``vector`` is the dense embedding.
    """

    node: NodeId
    model: str
    vector: tuple[float, ...]


def structural_entity_candidates(
    *,
    hypotheses: Iterable[NodeId],
    evidence: Iterable[NodeId],
    involves: Iterable[InvolvesRow],
) -> list[Candidate]:
    """Stage 1 (§5.1): pair every hypothesis with evidence sharing one of its ``INVOLVES`` entities.

    Pure (DB-free). An ``(evidence, hypothesis)`` pair is a candidate iff both nodes reference a
    common ``Actor``/``Object`` (role-agnostic co-occurrence — the near-free structural prior).
    Only ``involves`` rows whose ``node`` is in the given ``hypotheses``/``evidence`` sets count,
    so the caller's active-box scoping (§9) carries through; an entity links a hypothesis to
    *itself* or to another hypothesis is ignored (candidates run evidence → hypothesis only).

    Each emitted :class:`Candidate` carries the **full set** of entities the pair shares (so a
    pair sharing two entities is one candidate, its rationale complete), tagged
    :attr:`CandidateSource.STRUCTURAL_ENTITY`.
    """
    hyp_set = set(hypotheses)
    ev_set = set(evidence)

    # entity -> the hypotheses / evidence that reference it (active nodes only)
    by_entity_hyp: dict[NodeId, set[NodeId]] = {}
    by_entity_ev: dict[NodeId, set[NodeId]] = {}
    for row in involves:
        if row.node in hyp_set:
            by_entity_hyp.setdefault(row.entity, set()).add(row.node)
        elif row.node in ev_set:
            by_entity_ev.setdefault(row.entity, set()).add(row.node)

    # (evidence, hypothesis) -> the entities they share — accumulate then emit one candidate each.
    shared: dict[tuple[NodeId, NodeId], set[NodeId]] = {}
    for entity, hyps in by_entity_hyp.items():
        evs = by_entity_ev.get(entity)
        if not evs:
            continue
        for h in hyps:
            for e in evs:
                shared.setdefault((e, h), set()).add(entity)

    return [
        Candidate(
            evidence=e,
            hypothesis=h,
            sources=frozenset({CandidateSource.STRUCTURAL_ENTITY}),
            shared_entities=frozenset(entities),
        )
        for (e, h), entities in shared.items()
    ]


def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine of two equal-length dense vectors, in ``[-1, 1]`` (``0.0`` if either is the zero
    vector — undefined direction contributes no relatedness rather than raising)."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def embedding_knn_candidates(
    *,
    hypotheses: Iterable[EmbeddedNode],
    evidence: Iterable[EmbeddedNode],
    k: int = DEFAULT_K,
    min_similarity: float | None = DEFAULT_MIN_SIMILARITY,
) -> list[Candidate]:
    """Stage 2 (§5.1): pair every hypothesis with its ``k`` nearest evidence claims by cosine.

    Pure (DB-free). For each hypothesis node, evidence nodes are ranked by the **best** cosine
    similarity over their proposition vectors and the top ``k`` distinct nodes become candidates,
    directed evidence → hypothesis (the schema direction a ``SUPPORTS``/``REFUTES`` edge runs,
    §5/§10), tagged :attr:`CandidateSource.EMBEDDING_KNN`. The pair is **unscored** (recall-first;
    the cosine rank is a transient selection signal, not persisted — precision is the §8 LLM
    stage's job), so the embedding stage supplies no ``shared_entities`` rationale.

    **Vector-space identity guard (G1.16).** Two vectors are compared **only when their ``model``
    matches** — cosine across embedding models is meaningless. A hypothesis and a piece of evidence
    embedded under different models are simply never compared (no candidate), exactly as a span and
    a proposition in different spaces are kept apart at ingest.

    **The recall-first selection (the :data:`DEFAULT_MIN_SIMILARITY` decision).** With
    ``min_similarity`` unset (the default), selection is pure rank-based top-``k`` with **no
    distance floor**, so a dissimilar-but-real refuter inside the top ``k`` survives; passing a
    floor keeps only neighbours at least that similar (a precision pre-filter, never the default).

    Exact in-memory cosine over the active working set — the recall ceiling an approximate pgvector
    ANN index is later measured against (G4.6), and the seam where the ``<=>`` push-down replaces
    this loop without changing the contract. Determinism: ties break by descending similarity then
    node id, so a replay/trace is stable (§10) regardless of input order.
    """
    if k <= 0:
        return []

    # Group each side's vectors by node — a node may be EVIDENCED_BY several propositions.
    hyp_vecs: dict[NodeId, list[EmbeddedNode]] = {}
    for en in hypotheses:
        hyp_vecs.setdefault(en.node, []).append(en)
    ev_vecs: dict[NodeId, list[EmbeddedNode]] = {}
    for en in evidence:
        ev_vecs.setdefault(en.node, []).append(en)

    candidates: list[Candidate] = []
    for h, h_entries in hyp_vecs.items():
        # Best same-model cosine between this hypothesis and each evidence node (None = not
        # comparable: no shared vector space). A self-pair (h is also evidence) is skipped —
        # candidates run evidence → hypothesis only, never a node against itself.
        scored: list[tuple[float, NodeId]] = []
        for e, e_entries in ev_vecs.items():
            if e == h:
                continue
            best: float | None = None
            for he in h_entries:
                for ee in e_entries:
                    if he.model != ee.model:
                        continue
                    sim = _cosine_similarity(he.vector, ee.vector)
                    if best is None or sim > best:
                        best = sim
            if best is None:
                continue
            if min_similarity is not None and best < min_similarity:
                continue
            scored.append((best, e))

        scored.sort(key=lambda s: (-s[0], s[1]))
        for _sim, e in scored[:k]:
            candidates.append(
                Candidate(
                    evidence=e,
                    hypothesis=h,
                    sources=frozenset({CandidateSource.EMBEDDING_KNN}),
                )
            )
    return candidates


def funnel(
    *generators: Iterable[Candidate],
    strategy: FunnelStrategy = DEFAULT_STRATEGY,
) -> CandidatePool:
    """Combine the cheap generators' candidates into one deduped, deterministic pool (§5.1).

    Written once, generic over :class:`FunnelStrategy` (not branched per generator): each
    ``generators`` argument is one stage's output, merged by ``(evidence, hypothesis)`` pair with
    sources/entities **unioned** (a pair two stages found is one candidate carrying both sources).

    - :attr:`FunnelStrategy.UNION` (default) keeps every pair any generator proposed — recall-first
      (§5.1); the dissimilar refuter the embedding stage misses but the structural stage catches
      survives.
    - :attr:`FunnelStrategy.INTERSECT` keeps only pairs **every** non-empty generator proposed (a
      precision pre-filter; drops the dissimilar refuter — never the candidate-generation default).

    Output is sorted by pair so the trace is stable (§10).
    """
    # Merge all candidates by pair, unioning provenance, and track which generators proposed each.
    merged: dict[tuple[NodeId, NodeId], Candidate] = {}
    proposed_by: dict[tuple[NodeId, NodeId], int] = {}
    n_generators = 0
    for gen in generators:
        seen_this_gen: set[tuple[NodeId, NodeId]] = set()
        produced = False
        for cand in gen:
            produced = True
            key = cand.key
            prev = merged.get(key)
            if prev is None:
                merged[key] = cand
            else:
                merged[key] = Candidate(
                    evidence=key[0],
                    hypothesis=key[1],
                    sources=prev.sources | cand.sources,
                    shared_entities=prev.shared_entities | cand.shared_entities,
                )
            if key not in seen_this_gen:
                seen_this_gen.add(key)
                proposed_by[key] = proposed_by.get(key, 0) + 1
        if produced:
            n_generators += 1

    if strategy is FunnelStrategy.INTERSECT and n_generators > 1:
        # Keep only pairs every non-empty generator proposed (precision pre-filter, §5.1).
        merged = {k: v for k, v in merged.items() if proposed_by.get(k, 0) == n_generators}

    candidates = tuple(sorted(merged.values(), key=lambda c: c.key))
    return CandidatePool(candidates=candidates)


class CandidateGenerationAdapter:
    """Generates candidates from the active AGE subgraph (§5.1) — the boundary, DB-bound half.

    DB-free to construct; the reads happen in the ``async`` methods (lazy ``iknos.db.age``
    import). Stateless — a full current-state read per call, like the QBAF/derivation adapters.
    Reuses the shared ``load_active_box_ids`` / ``load_reasoning_nodes`` / ``load_hypothesis_ids``
    reads, so the active-subgraph definition is single-sourced (§9, §10).
    """

    async def _load_involves(self, session: object) -> list[InvolvesRow]:
        """All current ``INVOLVES`` edges between current nodes, with the entity ``role`` (§10).

        Both endpoints must be bitemporally current (``valid_to IS NULL``) — an edge to a
        retracted entity links nothing. Active-box scoping is applied in :meth:`generate` against
        the shared active-node universe (the entity's box is not re-read here).
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (n)-[r:INVOLVES]->(e) "
            "WHERE n.valid_to IS NULL AND e.valid_to IS NULL "
            "RETURN n.id, e.id, r.role",
            returns="nid agtype, eid agtype, role agtype",
        )
        return [
            InvolvesRow(node=unquote_agtype(nid), entity=unquote_agtype(eid), role=_opt_str(role))
            for nid, eid, role in rows
        ]

    async def _load_evidenced_propositions(self, session: object) -> list[tuple[NodeId, NodeId]]:
        """Each current reasoning node paired with a ``Proposition`` it is ``EVIDENCED_BY`` (§4).

        The node → claim provenance link the embedding stage rides: a reasoning node's dense vector
        is the row its source ``Proposition`` has in ``proposition_embeddings``. The node must be
        bitemporally current (``valid_to IS NULL``); the proposition is immutable content (no
        ``valid_to``), and ``(p:Proposition)`` label-matching keeps the claim-space ``EVIDENCED_BY``
        edges (node → Proposition) and excludes the text-locator ones (node → Span). Active-box
        scoping is applied in :meth:`generate` against the shared active-node universe.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (n)-[:EVIDENCED_BY]->(p:Proposition) WHERE n.valid_to IS NULL RETURN n.id, p.id",
            returns="nid agtype, pid agtype",
        )
        return [(unquote_agtype(nid), unquote_agtype(pid)) for nid, pid in rows]

    async def _load_proposition_vectors(
        self, session: object, proposition_ids: set[NodeId]
    ) -> dict[NodeId, list[tuple[str, tuple[float, ...]]]]:
        """Dense ``proposition_embeddings`` rows for the given propositions — the cross-store read.

        The vectors live in the relational pgvector store, the nodes in AGE; one shared session
        serves both (the engine bootstraps AGE *and* pgvector on every connection). Returns, per
        proposition id, its ``(model, vector)`` rows — normally one; more than one only
        mid-model-migration, and :func:`embedding_knn_candidates` keeps each in its own vector space
        (the G1.16 identity guard), so they are never silently mixed.
        """
        if not proposition_ids:
            return {}
        import uuid as _uuid

        from sqlalchemy import select

        from iknos.db.orm import PropositionEmbedding

        result = await session.execute(  # type: ignore[attr-defined]
            select(
                PropositionEmbedding.proposition_id,
                PropositionEmbedding.model,
                PropositionEmbedding.embedding,
            ).where(
                PropositionEmbedding.proposition_id.in_(
                    [_uuid.UUID(pid) for pid in proposition_ids]
                )
            )
        )
        by_prop: dict[NodeId, list[tuple[str, tuple[float, ...]]]] = {}
        for prop_id, model, embedding in result.all():
            by_prop.setdefault(str(prop_id), []).append((model, tuple(float(x) for x in embedding)))
        return by_prop

    async def generate(
        self,
        session: object,
        *,
        strategy: FunnelStrategy = DEFAULT_STRATEGY,
        k: int = DEFAULT_K,
    ) -> CandidatePool:
        """Read the active subgraph and run the funnel — the read path G4.3 consumes.

        Partitions the active reasoning nodes into hypotheses (the targets) and evidence
        (everything else — ``Fact``/``Conclusion``), then folds two cheap stages through
        :func:`funnel`: **stage 1** (the structural-entity prior, scoping the ``INVOLVES`` rows to
        the active universe) and **stage 2** (the embedding k-NN, tracing each active node to its
        ``EVIDENCED_BY`` proposition vector and proposing the ``k`` nearest per hypothesis). A node
        with no proposition embedding (e.g. an un-propositionized hypothesis stub) simply
        contributes no embedding candidate — the structural recall floor still covers it.
        (Coarse-to-fine is the further generator that unions in here without a contract change.)
        """
        active_box_ids = await load_active_box_ids(session)
        nodes = await load_reasoning_nodes(session)
        hyp_ids = await load_hypothesis_ids(session)
        involves = await self._load_involves(session)
        node_props = await self._load_evidenced_propositions(session)

        active_ids = {n.id for n in nodes if active_box_ids is None or n.box in active_box_ids}
        hypotheses = active_ids & hyp_ids
        evidence = active_ids - hyp_ids
        involves_active = [r for r in involves if r.node in active_ids]

        structural = structural_entity_candidates(
            hypotheses=hypotheses, evidence=evidence, involves=involves_active
        )

        # Stage 2: trace active node -> EVIDENCED_BY proposition -> pgvector row, then k-NN.
        node_props_active = [(n, p) for n, p in node_props if n in active_ids]
        vectors = await self._load_proposition_vectors(session, {p for _n, p in node_props_active})
        hyp_embedded: list[EmbeddedNode] = []
        ev_embedded: list[EmbeddedNode] = []
        for n, p in node_props_active:
            for model, vec in vectors.get(p, ()):
                bucket = hyp_embedded if n in hyp_ids else ev_embedded
                bucket.append(EmbeddedNode(node=n, model=model, vector=vec))
        embedding = embedding_knn_candidates(hypotheses=hyp_embedded, evidence=ev_embedded, k=k)

        return funnel(structural, embedding, strategy=strategy)


def _opt_str(v: object) -> str | None:
    """Parse an agtype scalar that may be SQL/agtype null into ``str | None`` (mirrors the
    derivation/QBAF adapters' null-tolerant parse; an ``INVOLVES`` ``role`` may be absent)."""
    if v is None or str(v) == "null":
        return None
    from iknos.db.age import unquote_agtype

    return unquote_agtype(v)
