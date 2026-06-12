"""`SAME_AS`-component evidence aggregation (Phase 3, G3.7; architecture §5.2, §12).

Entity identity is the **`SAME_AS`-connected component**, and *reasoning aggregates evidence
at the component level* (§5.2): the evidential standing of a real-world entity is the
aggregate over the reasoning nodes that mention **any** of its (possibly still un-merged)
duplicate mentions — not the standing of a single raw node. Under-merging fragments that
evidence so support never accumulates; this module is where, once G2.3 has drawn the
`SAME_AS` edges, the accumulation actually happens.

**The two annotations aggregate by their own algebra (§12), never merged into one number.**
For a canonical component:

* **support** (Layer A) accrues **additively** — the integer support-counts of the
  involving reasoning nodes are *summed*: more facts mentioning the entity (or its
  duplicates) ⇒ a higher component support. This is the counting/group side of §12.
* **confidence** (Layer B) accrues by the semiring **`⊕`** (`max` under the Gödel/Viterbi
  default) over those nodes' confidences — the *best* available evidence about the entity.
  `⊕` is idempotent and absorptive (§12), so re-presenting the same evidence (a re-run, an
  overlapping component) never inflates it.

**Merge and split are belief revision (§5.2).** A merge asserts a `SAME_AS`, a split
retracts one; either changes which canonical component a node's evidence accrues to, so the
correct response is simply to **re-aggregate** — "split the edge and the relationships it
created are re-evaluated automatically". In the current graph model a `SAME_AS` change does
not touch the `DERIVED_FROM`/`EVIDENCED_BY` structure (identity, not derivation grounding),
so the per-node Layer A/B values are unchanged and the revision is exactly this
re-aggregation; the contradiction→split-review loop that *lowers a wrong merge's confidence*
is the §6 `find-contradiction` path in Phase 4.

Pure/DB split (the ``core/resolve.py`` discipline): the aggregation algebra
(:func:`aggregate_components`) is DB-free and unit-testable; only :class:`ComponentReasoner`
touches AGE (the G3.4 adapter + the `INVOLVES`/`SAME_AS` reads), via lazy imports.

Scope deliberately left to later increments (documented seams):

- **Affected-component-only recompute** — §5.2/§12 want a merge/split to re-run Layer A/B
  over the *affected component only*. The MVP re-aggregates the whole active subgraph (small
  per investigation, §13); scoping the recompute to the touched component is the incremental
  refinement, paired with G3.3's persisted maintenance.
- **The contradiction→split-review loop + hysteresis** (§5.2, §6) — lowering a wrong merge's
  `SAME_AS` confidence when it manufactures a contradiction needs `find-contradiction` and
  the QBAF, so it is Phase 4 (and the composed-loop termination of G3.9).
- **Candidate (non-confirmed) `SAME_AS`** does *not* merge components (§5.2 conservative
  default), so it is excluded here exactly as in ``resolve.canonical_components``.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.confidence import DEFAULT_SEMIRING, Confidence, Semiring, valuate
from iknos.core.derivation_adapter import DerivationGraphAdapter
from iknos.core.truth_maintenance import IncrementalOracle, NodeId
from iknos.provenance.action_log import record_action
from iknos.types.edges import SameAsState

# Note: iknos.db.age and iknos.core.resolve are imported lazily inside the DB methods (the
# resolve.py discipline), so importing this module stays DB-free for the pure aggregation tests.

# Bump on any change to the aggregation/belief-revision contract; stored on each revise
# Action so the producing pipeline is identifiable (mirrors resolve.RESOLVE_SCHEMA_VERSION).
AGGREGATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ComponentEvidence:
    """The aggregated evidential standing of one canonical entity component (§5.2, §12).

    ``canonical`` is the component's representative entity id; ``members`` the
    `SAME_AS`-connected entity ids (a singleton ``{canonical}`` for an un-merged entity);
    ``nodes`` the supported reasoning nodes involving any member. The two §12 annotations are
    carried **separately**: ``support_count`` is the additive Layer A sum, ``confidence`` the
    `⊕`-aggregated Layer B best.
    """

    canonical: NodeId
    members: frozenset[NodeId]
    nodes: frozenset[NodeId]
    support_count: int
    confidence: Confidence


def aggregate_components(
    *,
    involves: Mapping[NodeId, frozenset[NodeId]],
    canonical_of: Mapping[NodeId, NodeId],
    support_count: Mapping[NodeId, int],
    confidence: Mapping[NodeId, Confidence],
    semiring: Semiring = DEFAULT_SEMIRING,
) -> dict[NodeId, ComponentEvidence]:
    """Aggregate per-node Layer A/B annotations to the canonical `SAME_AS` component — pure.

    Inputs (all keyed by the same stringified ids the reasoning core uses):

    * ``involves`` — reasoning node → the entity ids it ``INVOLVES``;
    * ``canonical_of`` — entity id → its canonical component representative (an entity absent
      from the map is its own canonical, i.e. an un-merged singleton);
    * ``support_count`` / ``confidence`` — Layer A / Layer B per reasoning node, over **only**
      the well-founded-supported nodes (an unsupported node contributes no evidence).

    Returns ``{canonical_entity: ComponentEvidence}`` for every canonical entity that some
    supported reasoning node involves. A node mentioning the same canonical twice (via two
    merged members) accrues **once** to it — membership in ``nodes`` is a set, so support is
    not double-counted within a node. Deterministic: components/members/nodes are built as
    sorted frozensets, the confidence fold is `⊕`-order-independent.
    """
    # canonical -> the set of supported reasoning nodes involving it, and its member ids.
    nodes_by_canonical: dict[NodeId, set[NodeId]] = {}
    members_by_canonical: dict[NodeId, set[NodeId]] = {}

    for node, entities in involves.items():
        if support_count.get(node, 0) <= 0:
            continue  # unsupported node carries no evidence (foundedness gate, §12)
        for entity in entities:
            canonical = canonical_of.get(entity, entity)
            nodes_by_canonical.setdefault(canonical, set()).add(node)
            members_by_canonical.setdefault(canonical, set()).add(entity)

    result: dict[NodeId, ComponentEvidence] = {}
    for canonical, nodes in nodes_by_canonical.items():
        total_support = sum(support_count[n] for n in nodes)  # Layer A: additive
        best = semiring.combine_alternatives(confidence.get(n, semiring.zero) for n in nodes)
        result[canonical] = ComponentEvidence(
            canonical=canonical,
            members=frozenset(members_by_canonical[canonical]),
            nodes=frozenset(nodes),
            support_count=total_support,
            confidence=best,
        )
    return result


def canonical_map(components: list[frozenset[NodeId]]) -> dict[NodeId, NodeId]:
    """Build the entity → canonical representative map from `SAME_AS` components — pure.

    The representative is the lexicographically-min id (matching ``resolve.canonical_id``),
    so the map is deterministic and stable across runs. Singletons need no entry — an entity
    absent from the map is its own canonical (:func:`aggregate_components` treats it so).
    """
    mapping: dict[NodeId, NodeId] = {}
    for component in components:
        rep = min(component)
        for member in component:
            mapping[member] = rep
    return mapping


@dataclass(frozen=True)
class RevisionResult:
    """The outcome of a merge/split belief revision: the Action id and the re-aggregation."""

    action_id: uuid.UUID
    components: dict[NodeId, ComponentEvidence]


class ComponentReasoner:
    """Aggregates Layer A/B evidence to `SAME_AS` components and runs merge/split revisions.

    DB-free to construct (it carries the G3.4 adapter + a semiring); the graph reads/writes
    happen in the methods. Stateless across calls. Mirrors ``core/resolve.py``'s boundary
    discipline — lazy ``iknos.db.age`` import, the pure aggregation delegated to
    :func:`aggregate_components`.
    """

    def __init__(
        self,
        *,
        adapter: DerivationGraphAdapter | None = None,
        semiring: Semiring = DEFAULT_SEMIRING,
    ) -> None:
        self.adapter = adapter or DerivationGraphAdapter()
        self.semiring = semiring

    async def aggregate(self, session: AsyncSession) -> dict[NodeId, ComponentEvidence]:
        """Aggregate the active subgraph's per-node Layer A/B annotations to canonical
        `SAME_AS` components (§5.2).

        Loads the active subgraph (G3.4), recomputes Layer A support (an
        :class:`IncrementalOracle` applied once) and Layer B confidence (:func:`valuate`) per
        node, reads the `INVOLVES` map and the **confirmed** `SAME_AS` components, then folds
        with :func:`aggregate_components`. Full recompute is the MVP (§13); affected-component
        scoping is the deferred refinement.
        """
        subgraph = await self.adapter.load_active(session)
        oracle = IncrementalOracle()
        supported = oracle.apply(subgraph.graph)
        support_count = {n: oracle.support_count(n) for n in supported}
        confidence = valuate(
            subgraph.graph,
            supported,
            base_confidence=subgraph.base_confidence,
            strength=subgraph.strength,
            semiring=self.semiring,
        )
        involves = await self._load_involves(session)
        components = await self._load_components(session)
        return aggregate_components(
            involves=involves,
            canonical_of=canonical_map(components),
            support_count=support_count,
            confidence=confidence,
            semiring=self.semiring,
        )

    async def merge(
        self,
        session: AsyncSession,
        a: uuid.UUID,
        b: uuid.UUID,
        *,
        box: uuid.UUID,
        strength: float,
    ) -> RevisionResult:
        """Assert a **confirmed** `SAME_AS` (a merge) and re-aggregate — a belief revision.

        The identity is written in canonical (min-id → max-id) direction (the
        ``resolve._persist_same_as`` key discipline) so it upserts; evidence about the two
        entities then accrues to one canonical component on the re-aggregation. Emits a
        ``belief-revision`` Action (§10.1). Cross-box `SAME_AS` belongs to the working box
        (§9), supplied as ``box``.
        """
        from iknos.core.resolve import same_as_to_props
        from iknos.db.age import merge_edge

        src, dst = sorted((a, b), key=str)
        now = datetime.now(UTC)
        await merge_edge(
            session,
            src_id=src,
            dst_id=dst,
            label="SAME_AS",
            props=same_as_to_props(
                box=box, state=SameAsState.CONFIRMED, strength=strength, now=now
            ),
        )
        return await self._revise(session, asserted=(src, dst), retracted=None)

    async def split(self, session: AsyncSession, a: uuid.UUID, b: uuid.UUID) -> RevisionResult:
        """Retract a `SAME_AS` (a split) by stamping ``valid_to``, then re-aggregate.

        The bitemporal retraction §5.2/§7.4 mandate (never a destructive delete): the edge
        leaves an auditable history, and the adapter/aggregation current-state filter drops
        it so the components separate again — "over-merging is recoverable". Emits a
        ``belief-revision`` Action (§10.1).
        """
        from iknos.db.age import execute_cypher

        src, dst = sorted((a, b), key=str)
        now = datetime.now(UTC).isoformat()
        await execute_cypher(
            session,
            f"MATCH (x {{id: '{src}'}})-[r:SAME_AS]->(y {{id: '{dst}'}}) "
            f"WHERE r.valid_to IS NULL SET r.valid_to = '{now}'",
        )
        return await self._revise(session, asserted=None, retracted=(src, dst))

    async def _revise(
        self,
        session: AsyncSession,
        *,
        asserted: tuple[uuid.UUID, uuid.UUID] | None,
        retracted: tuple[uuid.UUID, uuid.UUID] | None,
    ) -> RevisionResult:
        """Record the belief-revision Action and return the re-aggregation. One transaction."""
        from iknos.db.age import atomic_write

        components = await self.aggregate(session)
        # W7: the caller's SAME_AS valid_to SET (uncommitted on this session) + the revision Action
        # commit as one unit — a failed Action can never leave a retraction half-applied.
        async with atomic_write(session):
            action_id = await record_action(
                session,
                actor="belief-revision",
                action_type="revise_components",
                inputs={
                    "asserted": [str(asserted[0]), str(asserted[1])] if asserted else None,
                    "retracted": [str(retracted[0]), str(retracted[1])] if retracted else None,
                    "schema_version": AGGREGATE_SCHEMA_VERSION,
                },
                outputs={
                    "components": {
                        c.canonical: {"support_count": c.support_count, "confidence": c.confidence}
                        for c in components.values()
                    }
                },
            )
        return RevisionResult(action_id=action_id, components=components)

    async def _load_involves(self, session: AsyncSession) -> dict[NodeId, frozenset[NodeId]]:
        """Current reasoning node → the entity ids it ``INVOLVES`` (§10)."""
        from iknos.db.age import execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,
            "MATCH (n)-[:INVOLVES]->(e) WHERE n.valid_to IS NULL RETURN n.id, e.id",
            returns="nid agtype, eid agtype",
        )
        out: dict[NodeId, set[NodeId]] = {}
        for nid, eid in rows:
            out.setdefault(unquote_agtype(nid), set()).add(unquote_agtype(eid))
        return {k: frozenset(v) for k, v in out.items()}

    async def _load_components(self, session: AsyncSession) -> list[frozenset[NodeId]]:
        """The **confirmed**, current `SAME_AS` components, graph-wide (§5.2).

        Confirmed only — a candidate keeps entities separate (the conservative default); a
        ``valid_to``-stamped (split) edge is excluded by the current-state filter. Union-find
        via ``resolve.components`` (reused so the component logic cannot diverge).
        """
        from iknos.core.resolve import components as union_components
        from iknos.db.age import execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,
            f"MATCH (a)-[r:SAME_AS]->(b) WHERE r.state = '{SameAsState.CONFIRMED}' "
            "AND r.valid_to IS NULL RETURN a.id, b.id",
            returns="aid agtype, bid agtype",
        )
        pairs = [
            (uuid.UUID(unquote_agtype(aid)), uuid.UUID(unquote_agtype(bid))) for aid, bid in rows
        ]
        return [frozenset(str(m) for m in comp) for comp in union_components(pairs)]
