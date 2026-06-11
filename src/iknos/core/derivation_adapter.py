"""Phase 2 → reasoning-core adapter (G3.4; architecture §12, §7.1, §10).

The pure Layer A (``core/truth_maintenance.py``) and Layer B (``core/confidence.py``)
engines operate on an **abstract** :class:`~iknos.core.truth_maintenance.DerivationGraph`
of opaque ``NodeId`` strings — they know nothing of AGE, UUIDs, boxes, or bitemporal
validity. This module is the **boundary** that reads the persisted property graph and
produces exactly the three inputs those engines consume:

* a :class:`~iknos.core.truth_maintenance.DerivationGraph` — ``base_facts`` (the
  ``EVIDENCED_BY``-grounded reasoning nodes) and ``derivations`` (the ``DERIVED_FROM``
  groups), the Layer A grounding anchor and rules;
* ``base_confidence`` — each base fact's ``[0, 1]`` confidence property (the Layer B seed,
  §12), keyed by ``NodeId``;
* ``strength`` — each derivation's ``DERIVED_FROM`` edge confidence (§7.1), keyed by the
  same :class:`~iknos.core.truth_maintenance.Derivation` value Layer A uses.

**The active subgraph (§12, §10).** Reasoning is over the *current* belief state, so the
adapter selects only:

* **bitemporally current** nodes and edges — ``valid_to IS NULL`` (a retraction stamps
  ``valid_to`` rather than deleting, so a retracted node simply drops out of the load);
* nodes in **active** boxes — a deprecated box's nodes are excluded, and a derivation that
  rests on one loses support (its antecedent is not in the active set, so it never fires);

leaving ``SAME_AS``-component aggregation — accruing support/confidence to the canonical
entity and treating a merge/split as a belief-revision trigger — to **G3.7**, which builds
on this adapter. The selection is deliberately *partial*-tolerant: an antecedent that is
not itself an active reasoning node is simply unsupported (so are its dependents), exactly
as :class:`~iknos.core.truth_maintenance.DerivationGraph` documents — the loaded graph need
not be closed.

**The ``DERIVED_FROM`` grouping contract (defined here, written by the G3.8 operators).**
A conclusion may be grounded by more than one rule firing (a disjunction — losing one does
not drop it while another grounds it), and one rule firing is a conjunction over its whole
body. So a single ``DERIVED_FROM`` *edge* is not a derivation; a **group of edges sharing a
``derivation`` group-id** is. Every edge of one ``deduce``/``induce`` act carries the same
``derivation`` uuid and the same step ``strength`` (§7.1); this adapter regroups them into
:class:`~iknos.core.truth_maintenance.Derivation` bodies. (Absent a group-id — a
hand-written or legacy edge — the fallback groups a conclusion's edges into one body, the
safe conjunctive reading.)

Pure/DB split (the ``core/extract.py`` / ``core/resolve.py`` discipline): the **assembly**
(:func:`assemble_subgraph`, the row-grouping logic) is DB-free and unit-testable with
hand-built rows; only :class:`DerivationGraphAdapter`'s read methods touch AGE, and
``iknos.db.age`` is imported lazily inside them so importing this module never pulls in the
``DATABASE_URL`` config singleton.

Scope deliberately left to later increments (documented seams):

- **``SAME_AS``-component aggregation** (G3.7) — support/confidence accrue to the canonical
  component, and a merge/split re-runs Layer A/B over the affected component (§5.2, §12).
  This adapter loads raw reasoning nodes; it does not yet canonicalize them by entity.
- **Persisted / incremental maintenance** (G3.3) — this adapter does a *full* current-state
  read; the ``WITH RECURSIVE`` / IVM path that maintains the support set in Postgres as the
  graph changes is deferred (MVP is in-memory recompute over the small active subgraph, §13).
- **Per-investigation / box-scoped selection** — this loads the whole active subgraph
  across all active boxes (the well-defined current belief state). A box-scoped load that
  still pulls the cross-box antecedents a working-box derivation rests on is a richer
  selection deferred to the investigation runtime (Phase 6).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from iknos.core.confidence import (
    DEFAULT_SEMIRING,
    Confidence,
    Semiring,
    valuate,
)
from iknos.core.truth_maintenance import (
    Derivation,
    DerivationGraph,
    NodeId,
    RecomputeOracle,
    SupportOracle,
)

# The reasoning-node labels the derivation graph is built over (§10). Actor/Object are
# entities, Proposition/Span/Document are evidence/provenance, Box is governance — none are
# reasoning nodes, so none appear here. `DERIVED_FROM` connects reasoning nodes; a Fact is
# the only reasoning node grounded by `EVIDENCED_BY` (the base-fact anchor).
REASONING_LABELS: tuple[str, ...] = (
    "Fact",
    "DeductiveConclusion",
    "InductiveConclusion",
    "Hypothesis",
)


@dataclass(frozen=True)
class NodeRow:
    """One active reasoning node as read from AGE: its id, owning box, confidence seed.

    ``confidence`` is the node's ``[0, 1]`` Layer-B confidence property — for a base Fact
    the :func:`~iknos.core.extract.seed_confidence` value; it becomes ``base_confidence``
    for the nodes that turn out to be base facts. ``box`` is the owning box id (``None`` if
    a node carries none), used for the active-box filter.
    """

    id: NodeId
    box: str | None
    confidence: Confidence


@dataclass(frozen=True)
class DerivedRow:
    """One active ``DERIVED_FROM`` edge: ``conclusion`` is grounded *partly* by ``antecedent``.

    ``derivation`` is the group-id shared by every edge of the one rule firing this edge
    belongs to (so the adapter can reconstruct the conjunctive body); ``strength`` is that
    derivation's ``DERIVED_FROM`` edge confidence (§7.1), equal across the group's edges.
    """

    conclusion: NodeId
    antecedent: NodeId
    derivation: str | None
    strength: Confidence


@dataclass(frozen=True)
class ActiveSubgraph:
    """The three inputs the reasoning core consumes, assembled from the active graph.

    ``graph`` feeds Layer A (membership); ``base_confidence`` and ``strength`` feed Layer B
    (strength) over exactly the set Layer A certifies. The two annotations are never merged
    (§12): this carries them as separate maps, never one number.
    """

    graph: DerivationGraph
    base_confidence: dict[NodeId, Confidence] = field(default_factory=dict)
    strength: dict[Derivation, Confidence] = field(default_factory=dict)


def assemble_subgraph(
    nodes: Iterable[NodeRow],
    base_fact_ids: Iterable[NodeId],
    derived: Iterable[DerivedRow],
    *,
    active_box_ids: frozenset[str] | None = None,
) -> ActiveSubgraph:
    """Regroup raw AGE rows into an :class:`ActiveSubgraph` — the pure core of the adapter.

    DB-free, so unit-testable with hand-built rows. The steps:

    1. **Active-node universe.** Index ``nodes`` by id; a node is *active* iff
       ``active_box_ids`` is ``None`` (no box filter) or its box is in that set. A node not
       present in ``nodes`` at all (e.g. a non-reasoning antecedent) is not active.
    2. **Base facts.** The ``base_fact_ids`` (``EVIDENCED_BY``-grounded) that are active.
    3. **Derivations.** Group ``derived`` rows by ``derivation`` group-id (falling back to
       grouping by conclusion when a row carries none — the safe conjunctive reading). Each
       group becomes one :class:`Derivation` ``(conclusion, body=frozenset(antecedents))``,
       kept iff its conclusion is an active node. Antecedents are kept verbatim: an inactive
       antecedent stays in the body and is simply never supported (dropping it would wrongly
       make the rule *easier* to satisfy), so a derivation resting on a retracted or
       deprecated-box fact correctly fails to fire.
    4. **Side maps.** ``base_confidence`` is each base fact's node confidence; ``strength``
       is each derivation's edge strength (the group's representative — equal by the write
       contract; the **minimum** is taken if a stray group disagrees, the conservative
       weakest-link choice consistent with the Gödel default, §12).

    Ordering is deterministic (sorted) so the produced graph and any replay trace are
    stable regardless of row/set iteration order (§10).
    """
    node_box: dict[NodeId, str | None] = {}
    node_conf: dict[NodeId, Confidence] = {}
    for row in nodes:
        node_box[row.id] = row.box
        node_conf[row.id] = row.confidence

    def is_active(nid: NodeId) -> bool:
        if nid not in node_box:
            return False
        return active_box_ids is None or node_box[nid] in active_box_ids

    base_facts = frozenset(nid for nid in base_fact_ids if is_active(nid))

    # Group DERIVED_FROM rows into derivation bodies. A null group-id falls back to a
    # per-conclusion group so a conclusion's loose edges read as one conjunctive body.
    bodies: dict[object, set[NodeId]] = {}
    conclusions: dict[object, NodeId] = {}
    strengths: dict[object, list[Confidence]] = {}
    for edge in derived:
        key: object = (
            edge.derivation if edge.derivation is not None else ("by-conclusion", edge.conclusion)
        )
        bodies.setdefault(key, set()).add(edge.antecedent)
        conclusions[key] = edge.conclusion
        strengths.setdefault(key, []).append(edge.strength)

    derivations: list[Derivation] = []
    strength_map: dict[Derivation, Confidence] = {}
    for key, body in bodies.items():
        conclusion = conclusions[key]
        if not is_active(conclusion):
            continue
        deriv = Derivation(conclusion=conclusion, body=frozenset(body))
        derivations.append(deriv)
        # Equal across a group by the write contract; min is the safe pick if they diverge.
        strength_map[deriv] = min(strengths[key])

    derivations.sort(key=lambda d: (d.conclusion, tuple(sorted(d.body))))
    graph = DerivationGraph(base_facts=base_facts, derivations=tuple(derivations))
    base_confidence = {nid: node_conf[nid] for nid in base_facts}
    return ActiveSubgraph(graph=graph, base_confidence=base_confidence, strength=strength_map)


def support_and_confidence(
    subgraph: ActiveSubgraph,
    *,
    oracle: SupportOracle | None = None,
    semiring: Semiring = DEFAULT_SEMIRING,
) -> tuple[frozenset[NodeId], dict[NodeId, Confidence]]:
    """Run the two-layer seam over a loaded subgraph: Layer A certifies, Layer B scores.

    A thin convenience wiring the adapter's output through both engines (§12): Layer A
    (``oracle``, defaulting to a fresh :class:`RecomputeOracle`) returns the well-founded
    support set; Layer B (:func:`~iknos.core.confidence.valuate`) scores **exactly** that
    set with the loaded ``base_confidence``/``strength``. Returns ``(supported, confidence)``
    — the two annotations, separate (§12). The ``deduce``/``induce`` operators (G3.8) and the
    incremental path use the layers directly; this is the read-and-evaluate shortcut the
    integration test exercises.
    """
    oracle = oracle or RecomputeOracle()
    supported = oracle.well_founded_support(subgraph.graph)
    confidence = valuate(
        subgraph.graph,
        supported,
        base_confidence=subgraph.base_confidence,
        strength=subgraph.strength,
        semiring=semiring,
    )
    return supported, confidence


async def load_active_box_ids(session: object) -> frozenset[str]:
    """The ids of active (non-deprecated), current boxes (§9) — the active-box filter.

    Shared AGE read, reused by both the Phase-3 derivation adapter and the Phase-4 QBAF
    adapter (``core/qbaf_adapter.py``), so the "active box" definition cannot diverge between
    the propagation and adjudication loads.
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    rows = await execute_cypher(
        session,  # type: ignore[arg-type]
        "MATCH (b:Box) WHERE b.status = 'active' AND b.valid_to IS NULL RETURN b.id",
        returns="bid agtype",
    )
    return frozenset(unquote_agtype(bid) for (bid,) in rows)


async def load_reasoning_nodes(session: object) -> list[NodeRow]:
    """All bitemporally-current reasoning nodes, with box + confidence seed (§10, §12).

    One query per reasoning label (AGE matches a single label per pattern, like the per-kind
    loop in ``core/resolve.py``). A missing/null ``confidence`` defaults to the semiring ``one``
    (a certain leaf — "no calibrated discount yet", §12), the same seed
    ``extract.seed_confidence`` uses when no verifier ran. Shared with the QBAF adapter, which
    consumes the same node confidence as the QBAF intrinsic/base score (§12 seam).
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    rows: list[NodeRow] = []
    for label in REASONING_LABELS:
        raw = await execute_cypher(
            session,  # type: ignore[arg-type]
            f"MATCH (n:{label}) WHERE n.valid_to IS NULL RETURN n.id, n.box, n.confidence",
            returns="nid agtype, box agtype, conf agtype",
        )
        for nid, box, conf in raw:
            rows.append(
                NodeRow(
                    id=unquote_agtype(nid),
                    box=_opt_str(box),
                    confidence=_opt_float(conf, default=1.0),
                )
            )
    return rows


async def load_hypothesis_ids(session: object) -> set[NodeId]:
    """The ids of bitemporally-current ``Hypothesis`` nodes (§7.2, §10).

    A shared AGE read like :func:`load_active_box_ids` / :func:`load_reasoning_nodes`: the QBAF
    adapter uses it to pick the args that get a ``state``/verdict, and candidate generation
    (``core/candidates.py``) uses it to pick the *targets* evidence is paired against — so the
    "current Hypothesis" definition cannot diverge between adjudication and candidate generation.
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    rows = await execute_cypher(
        session,  # type: ignore[arg-type]
        "MATCH (h:Hypothesis) WHERE h.valid_to IS NULL RETURN h.id",
        returns="hid agtype",
    )
    return {unquote_agtype(hid) for (hid,) in rows}


class DerivationGraphAdapter:
    """Loads the active reasoning subgraph from AGE into an :class:`ActiveSubgraph`.

    DB-free to construct; the read happens in :meth:`load_active`. Stateless across calls —
    each load is a full current-state read (the incremental/persisted maintenance path is
    G3.3). Mirrors the ``core/resolve.py`` boundary discipline: lazy ``iknos.db.age`` import,
    pure assembly delegated to :func:`assemble_subgraph`.
    """

    async def _active_box_ids(self, session: object) -> frozenset[str]:
        return await load_active_box_ids(session)

    async def _load_nodes(self, session: object) -> list[NodeRow]:
        return await load_reasoning_nodes(session)

    async def _load_base_fact_ids(self, session: object) -> set[NodeId]:
        """The ids of current nodes grounded by ``EVIDENCED_BY`` — the Layer A base anchor.

        Only a ``Fact`` carries ``EVIDENCED_BY`` (to its Proposition/Span), so this is the
        base-fact set; the target node type is irrelevant (we only need that evidence exists).
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        raw = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (f)-[:EVIDENCED_BY]->() WHERE f.valid_to IS NULL RETURN DISTINCT f.id",
            returns="fid agtype",
        )
        return {unquote_agtype(fid) for (fid,) in raw}

    async def _load_derived(self, session: object) -> list[DerivedRow]:
        """All current ``DERIVED_FROM`` edges between current nodes, with group-id + strength."""
        from iknos.db.age import execute_cypher, unquote_agtype

        raw = await execute_cypher(
            session,  # type: ignore[arg-type]
            "MATCH (c)-[d:DERIVED_FROM]->(a) "
            "WHERE c.valid_to IS NULL AND a.valid_to IS NULL AND d.valid_to IS NULL "
            "RETURN c.id, a.id, d.derivation, d.strength",
            returns="cid agtype, aid agtype, deriv agtype, strength agtype",
        )
        return [
            DerivedRow(
                conclusion=unquote_agtype(cid),
                antecedent=unquote_agtype(aid),
                derivation=_opt_str(deriv),
                strength=_opt_float(strength, default=1.0),
            )
            for cid, aid, deriv, strength in raw
        ]

    async def load_active(self, session: object) -> ActiveSubgraph:
        """Read the active reasoning subgraph and assemble it for Layer A/B.

        Reads the active box set, the current reasoning nodes, the base-fact anchor, and the
        current ``DERIVED_FROM`` structure, then hands them to :func:`assemble_subgraph`.
        Pure once the four reads are done — the grouping/filtering logic is shared with the
        unit tests via that function.
        """
        active_box_ids = await self._active_box_ids(session)
        nodes = await self._load_nodes(session)
        base_fact_ids = await self._load_base_fact_ids(session)
        derived = await self._load_derived(session)
        return assemble_subgraph(nodes, base_fact_ids, derived, active_box_ids=active_box_ids)


def _opt_str(v: object) -> str | None:
    """Parse an agtype scalar that may be SQL/agtype null into ``str | None``."""
    if v is None or str(v) == "null":
        return None
    from iknos.db.age import unquote_agtype

    return unquote_agtype(v)


def _opt_float(v: object, *, default: float) -> float:
    """Parse an agtype number that may be null into ``float``, falling back to ``default``."""
    if v is None or str(v) == "null":
        return default
    return float(str(v))
