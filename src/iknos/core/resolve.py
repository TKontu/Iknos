"""The entity-resolution subsystem (Phase 2, G2.3; architecture.md §5.2, §6, §10).

G2.2 (``core/extract.py``) materializes a ``Fact`` per proposition with **fresh,
un-deduplicated** ``Actor``/``Object`` nodes: two mentions of the same real entity become
two nodes. G2.3 resolves those nodes into canonical entities.

Identity is a **defeasible, scored assertion**, never a destructive id reassignment (§5.2):
two entities are "the same" only via a scored ``SAME_AS`` edge, and the canonical entity is
the ``SAME_AS``-connected component — reasoning aggregates evidence at the component level.
Resolution is a cheap→expensive **cascade**: block candidates cheaply, score them on
relational/contextual evidence, resolve into components. The default is **conservative
under-merge** — over-merge fabricates contradictions and corrupts reasoning, so auto-merge
happens only above a high confidence bar (``CONFIRMED``); below it a ``CANDIDATE`` edge keeps
the entities separate but the fragmentation visible and the evidence bridgeable (§5.2).

Pure/DB split (the ``core/extract.py`` discipline): the cascade — ``normalize_label``,
``block_candidates``, ``score_pair``, ``decide``, ``same_as_to_props``, ``components`` — is
DB-free and unit-testable; ``iknos.db.age`` is imported lazily inside the ``Resolver`` DB
methods so importing this module never pulls in the ``DATABASE_URL`` config singleton.

Scope deliberately left to later slices (documented seams):

- **Blocking signals beyond lexical/type** — embedding-neighbourhood and taxonomy-anchor
  blocking (§5.2) need an entity-embedding store / G2.4–G2.5 entity-linking. This slice
  blocks on shared normalized tokens within a kind.
- **Anchor canonicalization (G2.8 slice 2, shipped here).** A case entity that
  **confirm**-anchors to the domain-pack taxonomy takes that taxonomy node as its canonical
  identity (anchor-first, §5.2/§14): :func:`anchored_components` folds the confirmed
  ``ANCHORS_TO`` map (``core/anchor``) into the ``SAME_AS`` components, and
  :meth:`Resolver.canonical_components` reads it. The remaining seam is **belief revision**
  on a re-anchor (re-run Layer A/B when the anchor changes) → Phase 3.
- **Merge/split as belief revision** — asserting/retracting a ``SAME_AS`` should re-run
  Layer A/B over the affected component (§12) → Phase 3. This slice writes the edges; it
  does not re-run reasoning.
- **The contradiction→split-review loop + hysteresis** (§5.2) needs ``find-contradiction``
  → Phase 4.
- **Cross-box ``SAME_AS``** (which belongs to the working box, §9) → this slice resolves
  **within a source box**; the caller passes one box's entities.
- **Expert-triage queue** for ``candidate`` merges → Phase 7. This slice records the
  ``state=candidate`` edge the queue will later consume.

Scoring is **deterministic and relational** (no LLM in the resolve path, §5.2: score on
shared facts/roles/attributes — *not* similarity; similarity is a blocking signal only).
"""

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.extract import NodeKind
from iknos.provenance.action_log import record_action
from iknos.types.edges import SameAsState

# Note: iknos.db.age is imported lazily inside the Resolver DB methods (see module
# docstring), so importing this module stays DB-free for the unit tests of the cascade.

# Bump on any change to the resolution pipeline (the cascade weights/bars, the blocking or
# scoring logic) — stored on each resolve Action so a SAME_AS edge's producing pipeline is
# identifiable (mirrors extract.EXTRACT_SCHEMA_VERSION).
RESOLVE_SCHEMA_VERSION = 1

# Decision bars (§5.2). CONFIRM is deliberately high (conservative under-merge); the band
# [CANDIDATE, CONFIRM) records a bridgeable candidate without committing identity.
RESOLVE_CONFIRM_BAR = 0.85
RESOLVE_CANDIDATE_BAR = 0.50

# Scoring weights — relational/contextual evidence (§5.2). Exact-attribute *agreement*
# (same normalized label / type) is legitimate evidence; fuzzy/embedding *similarity* is
# barred from scoring (it is a blocking signal only). A conflicting non-empty type is
# disconfirming (negative). Weights chosen so exact label + agreeing type alone lands in the
# candidate band (0.75) — never an auto-merge — and only added relational context confirms.
_W_LABEL = 0.50
_W_TYPE = 0.25
_W_REL = 0.35

_ARTICLES = frozenset({"the", "a", "an"})


def normalize_label(label: str) -> str:
    """Normalize a mention surface form for blocking + exact-agreement scoring.

    Lowercase, replace any run of non-alphanumerics with a single space, drop one leading
    article (``the``/``a``/``an``), trim. Deliberately trivial (no stemming/lemmatization):
    this is the *blocking* normalization and the exact-agreement key, not a similarity model.
    """
    s = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    parts = s.split()
    if parts and parts[0] in _ARTICLES:
        parts = parts[1:]
    return " ".join(parts)


@dataclass(frozen=True)
class EntityRecord:
    """One ``Actor``/``Object`` node plus the relational features resolution scores on.

    ``roles`` are the ``INVOLVES.role`` tags the entity plays across its facts; ``context``
    is the **relational fingerprint** — the normalized labels of the *other* entities that
    co-occur with it in a fact (§5.2 "shared facts/roles/attributes"). The fingerprint is
    label-based, not id-based, precisely because the co-occurring nodes are themselves
    un-resolved fresh nodes in this pass — two mentions of the same entity share context
    when they appear alongside same-labelled neighbours, which is genuine relational
    evidence rather than surface similarity of the entity itself.
    """

    id: uuid.UUID
    label: str
    type: str
    kind: NodeKind
    box: uuid.UUID
    roles: frozenset[str]
    context: frozenset[str]

    @property
    def norm(self) -> str:
        return normalize_label(self.label)

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self.norm.split())


def block_candidates(entities: list[EntityRecord]) -> list[tuple[EntityRecord, EntityRecord]]:
    """The cheap blocking stage: candidate pairs sharing ≥1 normalized token, same kind.

    An ``Actor`` is never ``SAME_AS`` an ``Object``, so pairs are formed within a ``NodeKind``
    only. Built via a token→entities inverted index (not the O(n²) all-pairs scan) and
    de-duplicated by the canonical (min-id, max-id) ordering. Box-scoping is structural: the
    caller passes one box's entities (§5.2 "within a source box, resolve locally").

    *Seam:* the embedding-neighbourhood and taxonomy-anchor blocking signals (§5.2) are
    deferred — this stage blocks on shared tokens only.
    """
    by_token: dict[tuple[NodeKind, str], list[EntityRecord]] = {}
    for e in entities:
        for tok in e.tokens:
            by_token.setdefault((e.kind, tok), []).append(e)

    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[EntityRecord, EntityRecord]] = []
    for bucket in by_token.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                key = tuple(sorted((str(a.id), str(b.id))))
                if key in seen:
                    continue
                seen.add(key)  # type: ignore[arg-type]
                pairs.append((a, b))
    return pairs


def _saturate(units: float) -> float:
    """Diminishing-returns map [0, ∞) → [0, 1): 0→0, 1→0.5, 2→0.75, 3→0.875.

    Relational evidence accumulates with diminishing returns — the first shared neighbour
    matters most, each subsequent one adds less.
    """
    return 1.0 - math.pow(0.5, units)


def score_pair(a: EntityRecord, b: EntityRecord) -> float:
    """Deterministic same-entity score in [0, 1] from relational/contextual evidence (§5.2).

    Evidence (similarity is **not** a signal here — it is blocking-only, §5.2):

    - **Attribute agreement (exact only).** Same normalized label, and same non-empty type
      (a conflicting non-empty type is disconfirming — negative contribution).
    - **Relational/contextual.** Shared context labels (the relational fingerprint) and a
      shared role, combined with diminishing returns.

    Weighted so exact label + agreeing type *alone* scores 0.75 — inside the candidate band,
    never an auto-merge — honouring the conservative under-merge default; relational context
    is what pushes a pair over the confirm bar.
    """
    label_exact = 1.0 if a.norm == b.norm else 0.0

    type_agree = 0.0  # missing type carries no information either way
    if a.type and b.type:
        type_agree = 1.0 if a.type == b.type else -1.0

    shared_context = len(a.context & b.context)
    shared_role = 1 if a.roles & b.roles else 0
    rel_signal = _saturate(shared_context + 0.5 * shared_role)

    score = _W_LABEL * label_exact + _W_TYPE * type_agree + _W_REL * rel_signal
    return max(0.0, min(1.0, score))


def decide(score: float) -> SameAsState | None:
    """Map a score to a resolution decision (§5.2 conservative default).

    ``>= RESOLVE_CONFIRM_BAR`` → ``CONFIRMED`` (auto-merge); ``>= RESOLVE_CANDIDATE_BAR`` →
    ``CANDIDATE`` (bridgeable, not committed); below → no edge (entities stay separate, no
    link recorded).
    """
    if score >= RESOLVE_CONFIRM_BAR:
        return SameAsState.CONFIRMED
    if score >= RESOLVE_CANDIDATE_BAR:
        return SameAsState.CANDIDATE
    return None


def same_as_to_props(
    *, box: uuid.UUID, state: SameAsState, strength: float, now: datetime
) -> dict[str, Any]:
    """Flatten a ``SAME_AS`` edge to AGE properties — the canonical write contract.

    The single place ``SAME_AS`` serialization lives (cf. ``extract.fact_to_props``,
    ``boxes/serde.box_to_props``). ``strength`` is the calibrated merge score (§8/§10); the
    **two §12 annotations** are seeded — ``support_count = 1`` (this one resolution act
    grounds the edge) and ``confidence`` from the strength (the Layer-B value is recomputed
    by Phase 3 when merge/split becomes belief revision). Bitemporal fields are stamped open
    (``valid_to``/``event_time`` null). The edge is symmetric; the caller writes it in a
    canonical endpoint direction so its ``merge_edge`` key is stable.
    """
    return {
        "box": str(box),
        "state": str(state),
        "strength": strength,
        "support_count": 1,
        "confidence": strength,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


def components(pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> list[frozenset[uuid.UUID]]:
    """Union-find over ``CONFIRMED`` ``SAME_AS`` pairs → canonical components (§5.2).

    Only confirmed edges merge (the caller filters; candidates keep entities separate). The
    result is the set of multi-member equivalence classes — the canonical entities reasoning
    aggregates evidence over. Singletons are omitted (an un-merged node is its own entity,
    needing no component record).
    """
    parent: dict[uuid.UUID, uuid.UUID] = {}

    def find(x: uuid.UUID) -> uuid.UUID:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(x: uuid.UUID, y: uuid.UUID) -> None:
        parent[find(x)] = find(y)

    for x, y in pairs:
        union(x, y)

    groups: dict[uuid.UUID, set[uuid.UUID]] = {}
    for node in parent:
        groups.setdefault(find(node), set()).add(node)
    return [frozenset(g) for g in groups.values() if len(g) > 1]


def canonical_id(component: frozenset[uuid.UUID]) -> uuid.UUID:
    """The deterministic canonical representative of a component (lexicographically-min id)."""
    return min(component, key=str)


def anchored_components(
    pairs: list[tuple[uuid.UUID, uuid.UUID]],
    anchors: Mapping[uuid.UUID, uuid.UUID],
) -> list["Component"]:
    """Fold ``CONFIRMED`` ``SAME_AS`` components with ``CONFIRMED`` anchors → canonical entities.

    The G2.8-slice-2 anchor-canonicalization read (§5.2/§14 *anchor first*). Two kinds of
    identity evidence are unioned into one equivalence:

    1. **``SAME_AS``** confirmed ``pairs`` — within-box relational identity (G2.3);
    2. **shared anchor** — case entities that confirm-anchor to the **same** taxonomy node are
       the same real-world thing (the taxonomy node *is* their identity), so they merge even
       with no ``SAME_AS`` between them. This is a read-time canonicalization over the already
       written ``ANCHORS_TO`` edges, **not** a new cross-box ``SAME_AS`` edge (that, in the
       working box, is Phase 6) — anchoring is the separate, authoritative cross-box identity
       mechanism (§9).

    ``anchors`` maps a case entity id (the ``ANCHORS_TO`` source — a label-canonical node) to
    its confirmed taxonomy node. A returned :class:`Component` is one canonical entity; its
    ``canonical`` is the taxonomy node when the component anchors to exactly one (an anchored
    *singleton* is still returned — its identity differs from its own id), else the min-id
    member. An un-anchored singleton is omitted (an un-merged node is its own entity, as in
    :func:`components`). A ``SAME_AS``-bridged multi-anchor conflict keeps the min-id
    representative and is surfaced via :attr:`Component.anchor_conflict`. Deterministic — the
    result is sorted by canonical id and a pure function of the inputs.
    """
    parent: dict[uuid.UUID, uuid.UUID] = {}

    def find(x: uuid.UUID) -> uuid.UUID:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(x: uuid.UUID, y: uuid.UUID) -> None:
        parent[find(x)] = find(y)

    for x, y in pairs:
        union(x, y)

    # Merge case entities sharing a confirmed taxonomy target (anchor canonicalizes).
    by_target: dict[uuid.UUID, list[uuid.UUID]] = {}
    for entity, target in anchors.items():
        find(entity)  # ensure an anchored singleton is a known node
        by_target.setdefault(target, []).append(entity)
    for entities in by_target.values():
        for other in entities[1:]:
            union(entities[0], other)

    groups: dict[uuid.UUID, set[uuid.UUID]] = {}
    for node in parent:
        groups.setdefault(find(node), set()).add(node)

    out: list[Component] = []
    for group in groups.values():
        members = frozenset(group)
        targets = frozenset(anchors[m] for m in members if m in anchors)
        if len(members) <= 1 and not targets:
            continue  # an un-anchored singleton is its own entity, no component record
        canonical = next(iter(targets)) if len(targets) == 1 else canonical_id(members)
        out.append(Component(canonical=canonical, members=members, anchors=targets))
    return sorted(out, key=lambda c: str(c.canonical))


@dataclass(frozen=True)
class SameAsEdge:
    """One resolved identity edge (canonical direction), as written/returned by the resolver."""

    src: uuid.UUID
    dst: uuid.UUID
    state: SameAsState
    strength: float


@dataclass(frozen=True)
class ResolveResult:
    """The outcome of resolving one box: the Action id and the edges written, by state."""

    action_id: uuid.UUID
    confirmed: list[SameAsEdge]
    candidate: list[SameAsEdge]


@dataclass(frozen=True)
class Component:
    """A canonical entity: its case-box members + the representative reasoning aggregates over.

    ``members`` are the ``CONFIRMED``-``SAME_AS``-connected case ``Actor``/``Object`` ids (a
    lone member when the entity links only by anchor). ``canonical`` is the representative:

    - **un-anchored** — the lexicographically-min member (``resolve.canonical_id``), the
      within-box component identity;
    - **anchored** — the **taxonomy node** the component ``CONFIRMED``-anchors to (§5.2/§14
      *anchor canonicalizes*): the authoritative cross-box identity, which is *not* itself a
      member (it lives in the pack box). Two case mentions that confirm-anchor to the same
      taxonomy node are therefore one canonical entity even without a ``SAME_AS`` between them.

    ``anchors`` is the set of distinct confirmed taxonomy targets the component carries: empty
    (un-anchored), a single node (anchored — that node is ``canonical``), or — only when a
    ``SAME_AS`` bridges two differently-anchored mentions — more than one, a surfaced
    ``anchor_conflict`` that keeps the min-id representative rather than silently picking a side.
    """

    canonical: uuid.UUID
    members: frozenset[uuid.UUID]
    anchors: frozenset[uuid.UUID] = frozenset()

    @property
    def anchored(self) -> bool:
        """The component has a single, unambiguous confirmed taxonomy anchor (its canonical)."""
        return len(self.anchors) == 1

    @property
    def anchor(self) -> uuid.UUID | None:
        """The taxonomy node this component canonicalizes to, or ``None`` if un-anchored or in
        anchor conflict."""
        return next(iter(self.anchors)) if len(self.anchors) == 1 else None

    @property
    def anchor_conflict(self) -> bool:
        """A ``SAME_AS`` merged mentions with *different* confirmed anchors — an open
        inconsistency to surface (the anchor cannot canonicalize), not auto-resolve."""
        return len(self.anchors) > 1


class Resolver:
    """The entity-resolution operator (§6): fresh ``Actor``/``Object`` nodes → ``SAME_AS``.

    DB-free to construct; the cascade is pure and the graph writes happen in the DB methods.
    Stateless across calls. Deterministic — re-running on an unchanged box recomputes the
    same edge set, and ``SAME_AS`` is written through the upsert ``merge_edge`` (keyed on
    endpoints+label), so a re-run writes no duplicates.
    """

    async def _load_entities(self, session: AsyncSession, box: uuid.UUID) -> list[EntityRecord]:
        """Load one box's ``Actor``/``Object`` nodes with their roles + relational context.

        Per kind, one query walks each entity's ``INVOLVES`` facts and the *other* entities
        co-involved in those facts, aggregating roles and the context fingerprint in Python.
        Every entity an extractor wrote has ≥1 ``INVOLVES`` edge, so the join reaches them all.
        """
        from iknos.db.age import unquote_agtype
        from iknos.db.cypher import CypherQuery, EdgeType, NodeLabel, node, rel

        bx = str(box)
        records: list[EntityRecord] = []
        for kind, label in ((NodeKind.ACTOR, NodeLabel.ACTOR), (NodeKind.OBJECT, NodeLabel.OBJECT)):
            rows = await (
                CypherQuery()
                .match(
                    node("f", NodeLabel.FACT)
                    + rel(EdgeType.INVOLVES, var="i")
                    + node("e", label, {"box": bx})
                )
                .optional_match(node("f") + rel(EdgeType.INVOLVES) + node("c", props={"box": bx}))
                .where("c.id <> e.id")
                .return_("e.id, e.label, e.type, i.role, c.label")
                .run(
                    session,
                    returns="eid agtype, label agtype, typ agtype, role agtype, clabel agtype",
                )
            )
            agg: dict[uuid.UUID, dict[str, Any]] = {}
            for eid, lab, typ, role, clabel in rows:
                key = uuid.UUID(unquote_agtype(eid))
                rec = agg.setdefault(
                    key,
                    {
                        "label": unquote_agtype(lab),
                        "type": unquote_agtype(typ),
                        "roles": set(),
                        "context": set(),
                    },
                )
                if role is not None and str(role) != "null":
                    rec["roles"].add(unquote_agtype(role))
                if clabel is not None and str(clabel) != "null":
                    rec["context"].add(normalize_label(unquote_agtype(clabel)))
            for key, rec in agg.items():
                records.append(
                    EntityRecord(
                        id=key,
                        label=rec["label"],
                        type=rec["type"],
                        kind=kind,
                        box=box,
                        roles=frozenset(rec["roles"]),
                        context=frozenset(rec["context"]),
                    )
                )
        return records

    async def _persist_same_as(
        self,
        session: AsyncSession,
        a: EntityRecord,
        b: EntityRecord,
        state: SameAsState,
        strength: float,
        box: uuid.UUID,
        now: datetime,
    ) -> SameAsEdge:
        """Write one ``SAME_AS`` edge in canonical (min-id → max-id) direction.

        ``merge_edge`` keys on (src, dst, label), so the canonical direction gives the
        symmetric edge one stable key — a re-run upserts rather than duplicating.
        """
        from iknos.db.cypher import EdgeType, merge_edge

        src, dst = sorted((a.id, b.id), key=str)
        await merge_edge(
            session,
            src_id=src,
            dst_id=dst,
            label=EdgeType.SAME_AS,
            props=same_as_to_props(box=box, state=state, strength=strength, now=now),
        )
        return SameAsEdge(src=src, dst=dst, state=state, strength=strength)

    async def resolve_box(self, session: AsyncSession, box: uuid.UUID) -> ResolveResult:
        """Resolve one box: load → block → score → decide → persist, with an ``Action``.

        The §6 operator shape. Emits one ``resolve`` Action (``actor="entity-resolver"``)
        naming the entities consulted and the edges written by state (§10.1), then commits.
        """
        from iknos.db.age import atomic_write

        now = datetime.now(UTC)
        entities = await self._load_entities(session, box)

        confirmed: list[SameAsEdge] = []
        candidate: list[SameAsEdge] = []
        # W7: all SAME_AS edges + the resolve Action as one unit.
        async with atomic_write(session):
            for a, b in block_candidates(entities):
                score = score_pair(a, b)
                state = decide(score)
                if state is None:
                    continue
                edge = await self._persist_same_as(session, a, b, state, score, box, now)
                (confirmed if state is SameAsState.CONFIRMED else candidate).append(edge)

            action_id = await record_action(
                session,
                actor="entity-resolver",
                action_type="resolve",
                inputs={
                    "box": str(box),
                    "entities": [str(e.id) for e in entities],
                    "schema_version": RESOLVE_SCHEMA_VERSION,
                },
                outputs={
                    "confirmed": [f"{e.src}->{e.dst}" for e in confirmed],
                    "candidate": [f"{e.src}->{e.dst}" for e in candidate],
                },
            )
        return ResolveResult(action_id=action_id, confirmed=confirmed, candidate=candidate)

    async def canonical_components(self, session: AsyncSession, box: uuid.UUID) -> list[Component]:
        """Read the box's canonical entities: ``CONFIRMED`` ``SAME_AS`` folded with anchors (§5.2).

        The read reasoning uses to aggregate evidence at the component level. ``CANDIDATE``
        ``SAME_AS``/``ANCHORS_TO`` edges are excluded — both keep entities separate by
        definition (the conservative default). A confirmed anchor canonicalizes (§14): the
        component takes its taxonomy node as the canonical identity, and two mentions anchored
        to the same taxonomy node fold into one entity (:func:`anchored_components`).
        """
        from iknos.core.anchor import EntityLinker
        from iknos.db.age import unquote_agtype
        from iknos.db.cypher import CypherQuery, EdgeType, lit, node, rel

        bx = str(box)
        rows = await (
            CypherQuery()
            .match(
                node("a", props={"box": bx})
                + rel(EdgeType.SAME_AS, var="r")
                + node("b", props={"box": bx})
            )
            .where("r.state = " + lit(SameAsState.CONFIRMED))
            .return_("a.id, b.id")
            .run(session, returns="aid agtype, bid agtype")
        )
        pairs = [
            (uuid.UUID(unquote_agtype(aid)), uuid.UUID(unquote_agtype(bid))) for aid, bid in rows
        ]
        anchors = await EntityLinker().anchored_targets(session, box)
        return anchored_components(pairs, anchors)
