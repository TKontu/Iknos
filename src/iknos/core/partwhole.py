"""The part-whole hierarchy / abstraction-level subsystem (Phase 2, G2.5; architecture.md
§14, §10, §6).

A fact attaches at a *level* of a domain's part-whole structure — "the gearbox had a bearing
failure" at *gearbox*, "the rolling surface shows particle indentations" at *roller*. **Level
is relative, derived, and a property of the referent, not the sentence** (§14): there is no
stored level scalar; a reasoning node's level is the position of its **subject-role**
``INVOLVES`` entity in the ``PART_OF`` order. This module builds that order over
``Actor``/``Object`` entities and derives level from it.

``PART_OF`` is **typed and split** (the W3C pattern, §14): ``directPartOf`` records each
direct decomposition step (intransitive); ``partOf`` is its transitive closure — and the
closure (the ancestor/roll-up view) runs **only along the transitivity-safe
component-integral subtype** (``edges.is_transitive``). Member-collection / portion-mass /
stuff-object meronymy are tagged and excluded from blanket roll-up, or wrong aggregations
leak into coarse-level views (§14).

**Acquisition — anchor first, induce only as fallback (§14).** Anchoring a referent to a
domain-pack taxonomy (entity linking) is the reliable, high-confidence path; text-induced
meronymy is the domain-fragile gap-filler, lower-confidence and human-review-gated. This
slice ships the **induce path**: the LLM emits ``directPartOf`` candidates from compositional
cues ("Y of X", possessives, "part of", "consists of"), tagged ``provenance=induced``.

Pure/DB split (the ``core/resolve.py`` discipline): the cycle-safe transitive closure
(:func:`transitive_closure`), the level read (:func:`derived_level`), and the write contracts
are DB-free and unit-testable; ``iknos.db.age`` is imported lazily in the operator's DB
methods.

Scope deliberately left to later slices (documented seams):

- **Anchoring to the pack taxonomy** (the *primary*, reliable path, §14) — needs entity
  linking → with G2.3/G2.4 anchor-canonicalization. This slice runs the §9.1 "induce-mode"
  (everything provisional), which is the correct cold-start behaviour, not a gap.
- **Relative ordering (last resort)** — containment cues + co-occurrence/degree asymmetry +
  the §2 chunk prior when no parent is named (§14 step 3).
- **Continuous level / intrinsic IC** — anchored depth + a Seco-style information-content
  score, and box-embedding / ConE generality for out-of-taxonomy entities (§14). This slice's
  level is the structure-only **partonomy depth** (ancestor count); embedding cosine and
  lexical concreteness are explicitly the *wrong* proxies (§14) and are never used.
- **Coverage policy metric** (fraction of referents that anchor) — needs anchoring to exist.
- **Belief-revision / retraction** of induced edges (re-run Layer A/B; clean stale ``partOf``)
  → Phase 3. This slice's closure is monotonic per run.
- **Merge with anchored structure / multi-pack taxonomy conflict resolution** (§14/§9) → with
  anchoring.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.extract import NodeKind
from iknos.core.llm import LLMClient
from iknos.core.prompts import vocab
from iknos.core.reference import Referent, group_referents
from iknos.core.resolve import normalize_label
from iknos.provenance.action_log import record_action
from iknos.types.edges import AttachmentProvenance, MeronymyType, Role, is_transitive

# Note: iknos.db.age is imported lazily inside the inducer's DB methods (see module
# docstring), so importing this module stays DB-free for the unit tests of the algorithms.

# Bump on any change to the induction pipeline (the detection prompt/schema, the closure or
# level logic). Stored on each induce Action so a directPartOf/partOf edge's producing
# pipeline is identifiable (mirrors reference.REFER_SCHEMA_VERSION).
PARTWHOLE_SCHEMA_VERSION = 1

# Text-induced meronymy is the weakest off-the-shelf step (§14): lower-confidence and
# human-review-gated. A fixed induced-edge confidence seed — the calibrated per-edge value is
# a review/Trial-A4 concern; anchored edges (deferred) carry a higher one.
INDUCED_CONFIDENCE = 0.5


class _PartOfOut(BaseModel):
    """One induced part-whole relation as emitted by the detector (drives guided decoding).

    ``child`` is the **part**, ``parent`` the **whole** (a roller's parent is the bearing).
    ``meronymy_type`` defaults to the only transitivity-safe subtype so a bare
    ``{"child": …, "parent": …}`` validates to a roll-up-eligible edge; the detector
    down-tags member/portion/stuff relations explicitly.
    """

    child: str
    parent: str
    meronymy_type: MeronymyType = MeronymyType.COMPONENT_INTEGRAL


class InducedMeronymy(BaseModel):
    """Structured-output contract for one proposition's part-whole relations; guided decoding."""

    relations: list[_PartOfOut]


MERONYMY_SCHEMA = InducedMeronymy.model_json_schema()


SYSTEM_PROMPT = (
    "You detect PART-WHOLE (meronymy) relations between the physical/structural entities a "
    "single statement mentions, so a part-whole hierarchy can be built. Detect a relation "
    "ONLY when the statement gives a compositional cue:\n"
    "- 'X is part of Y' / 'Y consists of X' / 'Y contains X';\n"
    "- 'the X of the Y' or possessive 'the Y's X' (the bearing of the gearbox);\n"
    "- a compositional noun phrase naming a part within a whole "
    "('high speed shaft locating bearing').\n"
    "Do NOT infer relations from world knowledge the statement does not state, and do NOT "
    "emit is-a/type relations (a bearing is-a component is NOT part-whole). If there is no "
    "compositional cue, return an empty list. Use the statement's own surface forms.\n"
    "Per relation: `child` is the PART, `parent` is the WHOLE (a roller's parent is its "
    "bearing).\n"
    f"- meronymy_type ({vocab(MeronymyType)}): use component-integral for a structural "
    "component of an object (gearbox/shaft/bearing/roller); member-collection for a member "
    "of a group; portion-mass/stuff-object for material portions; the others as they fit.\n"
    'Example: "The bearing of the gearbox overheated." -> {"relations": ['
    '{"child": "bearing", "parent": "gearbox", "meronymy_type": "component-integral"}]}.\n'
    'Return JSON of the form {"relations": [{"child": "...", "parent": "...", '
    '"meronymy_type": "..."}]}.'
)


def build_messages(statement: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one proposition's meronymy induction."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"STATEMENT:\n{statement}"},
    ]


@dataclass(frozen=True)
class DirectPartOf:
    """One induced direct decomposition step: ``child`` (part) → ``parent`` (whole)."""

    child: uuid.UUID
    parent: uuid.UUID
    meronymy_type: MeronymyType


def _acyclic_edges(
    edges: list[tuple[uuid.UUID, uuid.UUID]],
) -> tuple[list[tuple[uuid.UUID, uuid.UUID]], frozenset[uuid.UUID]]:
    """Split child→parent edges into the acyclic subset + the nodes caught in a cycle.

    A meronymy **cycle** (X part-of Y part-of X) is a contradiction, not a hierarchy — rolling
    up through it is meaningless — so closure must exclude it (§14: forms a DAG). Kahn's
    algorithm peels nodes with no remaining outgoing (child→parent) edge; whatever cannot be
    peeled sits on a cycle. Edges incident to a cyclic node are dropped from roll-up and the
    nodes returned so the caller can flag them (an unstable partonomy region). Deterministic:
    independent of input edge order.
    """
    nodes: set[uuid.UUID] = set()
    for c, p in edges:
        nodes.add(c)
        nodes.add(p)
    # out-degree = number of parents (child→parent direction we peel toward the roots/wholes)
    parents: dict[uuid.UUID, set[uuid.UUID]] = {n: set() for n in nodes}
    children: dict[uuid.UUID, set[uuid.UUID]] = {n: set() for n in nodes}
    for c, p in edges:
        if c == p:  # a self-loop is a degenerate cycle
            continue
        parents[c].add(p)
        children[p].add(c)

    out_deg = {n: len(parents[n]) for n in nodes}
    # Peel nodes whose every parent is already peeled (a leaf in the child→parent DAG view).
    queue = sorted((n for n in nodes if out_deg[n] == 0), key=str)
    peeled: set[uuid.UUID] = set()
    while queue:
        n = queue.pop()
        peeled.add(n)
        for child in sorted(children[n], key=str):
            out_deg[child] -= 1
            if out_deg[child] == 0:
                queue.append(child)

    cyclic = frozenset(nodes - peeled)
    acyclic = [(c, p) for c, p in edges if c != p and c not in cyclic and p not in cyclic]
    return acyclic, cyclic


def transitive_closure(
    edges: list[tuple[uuid.UUID, uuid.UUID]],
) -> tuple[set[tuple[uuid.UUID, uuid.UUID]], frozenset[uuid.UUID]]:
    """Cycle-safe transitive closure of ``directPartOf`` (child→parent) → ``partOf`` pairs.

    Returns ``(closure, cyclic_nodes)``: every ``(descendant, ancestor)`` reachable along the
    **acyclic** component-integral edges (the caller filters to that subtype), plus the nodes
    excluded because they sit on a meronymy cycle (§14). Memoized DFS over the acyclic DAG, so
    a node's ancestor set is computed once; safe because cycles were removed first. The result
    is a pure function of the edge set — re-running induction recomputes the same closure.
    """
    acyclic, cyclic = _acyclic_edges(edges)

    parents: dict[uuid.UUID, set[uuid.UUID]] = {}
    for c, p in acyclic:
        parents.setdefault(c, set()).add(p)

    memo: dict[uuid.UUID, set[uuid.UUID]] = {}

    def ancestors(node: uuid.UUID) -> set[uuid.UUID]:
        if node in memo:
            return memo[node]
        acc: set[uuid.UUID] = set()
        for parent in parents.get(node, ()):  # direct parents
            acc.add(parent)
            acc |= ancestors(parent)  # acyclic → terminates, no revisit guard needed
        memo[node] = acc
        return acc

    closure: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for node in parents:
        for anc in ancestors(node):
            closure.add((node, anc))
    return closure, cyclic


def derived_level(closure: set[tuple[uuid.UUID, uuid.UUID]], node: uuid.UUID) -> int:
    """A node's **partonomy depth** = its component-integral ancestor count (§14).

    The structure-only level: depth 0 is the coarsest (a whole with no parent — management
    view), higher depth is finer (expert view). Well-defined on a DAG (an entity with several
    parents still has one ancestor *set*). The continuous intrinsic-IC refinement (§14) is a
    deferred seam; this is the depth term it scales.
    """
    return sum(1 for c, _a in closure if c == node)


# --- write contracts -------------------------------------------------------


def direct_part_of_props(
    *,
    box: uuid.UUID,
    meronymy_type: MeronymyType,
    provenance: AttachmentProvenance,
    confidence: float,
    now: datetime,
) -> dict[str, Any]:
    """Flatten a ``directPartOf`` edge to AGE properties — the canonical write contract.

    Mirrors ``resolve.same_as_to_props``: the meronymy ``type`` tag + ``provenance`` (§14),
    the two §12 annotations seeded (``support_count = 1`` — this one induction act grounds the
    edge; ``confidence`` the calibrated induced/anchored value), and open bitemporal fields.
    Defeasible and overridable like every edge (§10.3); retraction is belief revision (Phase 3).
    """
    return {
        "box": str(box),
        "meronymy_type": str(meronymy_type),
        "provenance": str(provenance),
        "support_count": 1,
        "confidence": confidence,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


def part_of_props(
    *, box: uuid.UUID, provenance: AttachmentProvenance, confidence: float, now: datetime
) -> dict[str, Any]:
    """Flatten a ``partOf`` (transitive-closure) edge to AGE properties.

    The closure edge is always the transitivity-safe ``component-integral`` subtype (only it
    rolls up, §14), so no per-edge meronymy tag is written; it carries ``provenance`` and the
    two annotations like ``directPartOf``.
    """
    return {
        "box": str(box),
        "meronymy_type": str(MeronymyType.COMPONENT_INTEGRAL),
        "provenance": str(provenance),
        "support_count": 1,
        "confidence": confidence,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


@dataclass(frozen=True)
class InduceInput:
    """One unit of work: a proposition (id + text). Meronymy is induced from the text's cues."""

    proposition_id: uuid.UUID
    text: str


@dataclass(frozen=True)
class InduceResult:
    """The outcome of inducing one box: the last Action id, the directPartOf edges written, the
    partOf closure size, and the entities flagged on a meronymy cycle."""

    action_id: uuid.UUID | None
    direct: list[DirectPartOf]
    part_of_count: int
    cyclic: frozenset[uuid.UUID]


class MeronymyInducer:
    """The induce operator (§6, §14): proposition text → ``directPartOf`` + ``partOf`` closure.

    DB-free to construct; the LLM does **detection** only and the closure/level math is pure.
    Box-scoped and three-phase like the reference binder (the shared session is unsafe for
    concurrent use): (1) serial Action-log idempotency filter, (2) concurrent detection holding
    no DB session, (3) serial per-proposition persist of ``directPartOf``; then a final
    box-wide ``partOf`` closure recompute.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        sampling: dict[str, object] | None = None,
        concurrency: int = 8,
    ) -> None:
        self.llm = llm
        self.sampling = sampling or {"temperature": 0.0}
        self.concurrency = concurrency

    async def _detect(self, sem: asyncio.Semaphore, statement: str) -> list[_PartOfOut]:
        """Detect one statement's part-whole relations via guided decoding (LLM, DB-free)."""
        messages = build_messages(statement)
        async with sem:
            raw = await self.llm.guided_complete(messages, MERONYMY_SCHEMA, self.sampling)
        return InducedMeronymy.model_validate(raw).relations

    async def _load_referents(self, session: AsyncSession, box: uuid.UUID) -> list[Referent]:
        """Load the box's entities as canonical referents (the directPartOf endpoints).

        Reuses the reference-binding referent grouping (same-label fresh nodes collapse to one
        canonical id), so an induced relation connects canonical entities, robust to whether
        resolution (G2.3) has run.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        bx = str(box)
        rows_acc: list[tuple[uuid.UUID, str, str, NodeKind]] = []
        for kind, label in ((NodeKind.ACTOR, "Actor"), (NodeKind.OBJECT, "Object")):
            rows = await execute_cypher(
                session,
                f"MATCH (e:{label} {{box: '{bx}'}}) RETURN e.id, e.label, e.type",
                returns="eid agtype, lab agtype, typ agtype",
            )
            for eid, lab, typ in rows:
                rows_acc.append(
                    (uuid.UUID(unquote_agtype(eid)), unquote_agtype(lab), unquote_agtype(typ), kind)
                )
        return group_referents(rows_acc)

    async def _load_propositions(self, session: AsyncSession, box: uuid.UUID) -> list[InduceInput]:
        """Load the box's propositions (reached via the box Facts ``EVIDENCED_BY`` them)."""
        from iknos.db.age import execute_cypher, unquote_agtype

        bx = str(box)
        rows = await execute_cypher(
            session,
            f"MATCH (f:Fact {{box: '{bx}'}})-[:EVIDENCED_BY]->(p:Proposition) RETURN p.id, p.text",
            returns="pid agtype, ptext agtype",
        )
        seen: dict[uuid.UUID, InduceInput] = {}
        for pid, ptext in rows:
            key = uuid.UUID(unquote_agtype(pid))
            seen.setdefault(key, InduceInput(proposition_id=key, text=unquote_agtype(ptext)))
        return list(seen.values())

    async def _already_induced(self, session: AsyncSession, proposition_id: uuid.UUID) -> bool:
        """Whether this proposition's meronymy was already induced (idempotency, Action-backed)."""
        row = await session.execute(
            text(
                "SELECT 1 FROM actions WHERE actor = 'meronymy-inducer' AND action_type = 'induce' "
                "AND inputs->>'proposition' = :pid LIMIT 1"
            ),
            {"pid": str(proposition_id)},
        )
        return row.scalar_one_or_none() is not None

    def _resolve_endpoints(
        self, relations: list[_PartOfOut], by_norm: dict[str, Referent]
    ) -> list[DirectPartOf]:
        """Map detected (child, parent) surface labels to canonical entity ids (pure).

        A relation is kept only when **both** endpoints resolve to a box entity and they are
        distinct — an induced part-whole over an entity not in the graph, or a self-loop, is
        dropped (no node to attach, or a degenerate cycle).
        """
        out: list[DirectPartOf] = []
        for r in relations:
            child = by_norm.get(normalize_label(r.child))
            parent = by_norm.get(normalize_label(r.parent))
            if child is None or parent is None or child.canonical == parent.canonical:
                continue
            out.append(
                DirectPartOf(
                    child=child.canonical, parent=parent.canonical, meronymy_type=r.meronymy_type
                )
            )
        return out

    async def _persist_direct(
        self,
        session: AsyncSession,
        item: InduceInput,
        box: uuid.UUID,
        direct: list[DirectPartOf],
    ) -> uuid.UUID:
        """Persist one proposition's ``directPartOf`` edges + an ``induce`` Action (one txn)."""
        from iknos.db.age import merge_edge

        now = datetime.now(UTC)
        for d in direct:
            await merge_edge(
                session,
                src_id=d.child,
                dst_id=d.parent,
                label="directPartOf",
                props=direct_part_of_props(
                    box=box,
                    meronymy_type=d.meronymy_type,
                    provenance=AttachmentProvenance.INDUCED,
                    confidence=INDUCED_CONFIDENCE,
                    now=now,
                ),
            )
        action_id = await record_action(
            session,
            actor="meronymy-inducer",
            action_type="induce",
            inputs={
                "proposition": str(item.proposition_id),
                "box": str(box),
                "schema_version": PARTWHOLE_SCHEMA_VERSION,
            },
            outputs={
                "direct_part_of": [
                    {"child": str(d.child), "parent": str(d.parent), "type": str(d.meronymy_type)}
                    for d in direct
                ]
            },
            model=self.llm.model,
            sampling=self.sampling,
        )
        await session.commit()
        return action_id

    async def _rebuild_closure(
        self, session: AsyncSession, box: uuid.UUID
    ) -> tuple[int, frozenset[uuid.UUID]]:
        """Recompute the box's ``partOf`` closure over component-integral ``directPartOf`` edges.

        Loads every transitivity-safe ``directPartOf`` in the box, computes the cycle-safe
        closure (:func:`transitive_closure`), and upserts a ``partOf`` edge per
        ``(descendant, ancestor)`` pair via ``merge_edge`` (structurally idempotent — a re-run
        recomputes the same closure and writes no duplicate). Returns the closure size and the
        cyclic entities (flagged, excluded from roll-up). Non-transitive subtypes are loaded
        for nothing here — they are excluded by the §14 rule (``is_transitive``).
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        bx = str(box)
        rows = await execute_cypher(
            session,
            f"MATCH (c {{box: '{bx}'}})-[r:directPartOf]->(p {{box: '{bx}'}}) "
            "RETURN c.id, p.id, r.meronymy_type",
            returns="cid agtype, pid agtype, mtype agtype",
        )
        edges: list[tuple[uuid.UUID, uuid.UUID]] = []
        for cid, pid, mtype in rows:
            if is_transitive(MeronymyType(unquote_agtype(mtype))):
                edges.append((uuid.UUID(unquote_agtype(cid)), uuid.UUID(unquote_agtype(pid))))

        closure, cyclic = transitive_closure(edges)
        if closure:
            from iknos.db.age import merge_edge

            now = datetime.now(UTC)
            for descendant, ancestor in closure:
                await merge_edge(
                    session,
                    src_id=descendant,
                    dst_id=ancestor,
                    label="partOf",
                    props=part_of_props(
                        box=box,
                        provenance=AttachmentProvenance.INDUCED,
                        confidence=INDUCED_CONFIDENCE,
                        now=now,
                    ),
                )
            await session.commit()
        return len(closure), cyclic

    async def induce_box(self, session: AsyncSession, box: uuid.UUID) -> InduceResult:
        """Induce the box's part-whole hierarchy: detect ``directPartOf`` → rebuild ``partOf``.

        The §6 operator shape, box-scoped. Detects meronymy from each not-yet-induced
        proposition (concurrent, DB-free), maps endpoints to canonical entities, persists
        ``directPartOf``, then recomputes the box-wide ``partOf`` closure. Idempotent on
        settled propositions; the closure recompute is structurally idempotent.
        """
        referents = await self._load_referents(session, box)
        by_norm = {r.norm: r for r in referents}
        items = await self._load_propositions(session, box)

        # Phase 1: idempotency filter.
        pending: list[InduceInput] = []
        for item in items:
            if not await self._already_induced(session, item.proposition_id):
                pending.append(item)

        # Phase 2: concurrent detection, DB-free.
        sem = asyncio.Semaphore(self.concurrency)
        detected = await asyncio.gather(*(self._detect(sem, item.text) for item in pending))

        # Phase 3: serial persist of directPartOf, one transaction per proposition.
        last_action: uuid.UUID | None = None
        all_direct: list[DirectPartOf] = []
        for item, relations in zip(pending, detected, strict=True):
            direct = self._resolve_endpoints(relations, by_norm)
            last_action = await self._persist_direct(session, item, box, direct)
            all_direct.extend(direct)

        # Final: rebuild the transitive closure over the box's component-integral edges.
        part_of_count, cyclic = await self._rebuild_closure(session, box)
        return InduceResult(
            action_id=last_action,
            direct=all_direct,
            part_of_count=part_of_count,
            cyclic=cyclic,
        )

    async def _canonical_map(
        self, session: AsyncSession, box: uuid.UUID
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Map every box entity id → its label-canonical id (the directPartOf endpoint).

        The ``partOf`` edges connect canonical (label-grouped, min-id) entities, but a fact's
        subject-role ``INVOLVES`` may point at a *different* fresh node of the same entity
        (G2.2 emits one per mention). The level read must resolve through the **same**
        grouping the inducer used, so this rebuilds it (:func:`group_referents`) and inverts
        each component to its canonical. (When entity resolution, G2.3, has run, that canonical
        coincides with the ``SAME_AS`` component's min-id representative — they agree by
        construction; folding ``SAME_AS`` into this map is a later refinement.)
        """
        referents = await self._load_referents(session, box)
        return {member: r.canonical for r in referents for member in r.ids}

    async def _ancestor_count(self, session: AsyncSession, canonical: uuid.UUID) -> int:
        from iknos.db.age import execute_cypher

        rows = await execute_cypher(
            session,
            f"MATCH (e {{id: '{canonical}'}})-[:partOf]->(a) RETURN count(a)",
            returns="n agtype",
        )
        return int(str(rows[0][0])) if rows else 0

    async def entity_level(
        self, session: AsyncSession, box: uuid.UUID, entity_id: uuid.UUID
    ) -> int:
        """The partonomy depth of an entity = its ``partOf`` ancestor count (§14, derived).

        ``entity_id`` is resolved to its label-canonical id first (:meth:`_canonical_map`), so
        any fresh node of the entity reports the same depth. Depth 0 is the coarsest level.
        """
        cmap = await self._canonical_map(session, box)
        return await self._ancestor_count(session, cmap.get(entity_id, entity_id))

    async def fact_level(
        self, session: AsyncSession, box: uuid.UUID, fact_id: uuid.UUID
    ) -> list[int]:
        """A fact's derived abstraction level(s) (§14): the depth of its subject-role referent.

        Level is the position of the fact's **primary referent** — the entity on its
        ``subject``-role ``INVOLVES`` edge — in the ``partOf`` order (§14). A fact with several
        subject-role entities yields **several** levels: ambiguity is represented as
        uncertain/multiple, never forced to one value (§14). Each subject is resolved to its
        label-canonical id before its depth is counted. Returns the sorted distinct depths;
        empty when the fact has no subject-role referent.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        rows = await execute_cypher(
            session,
            f"MATCH (f:Fact {{id: '{fact_id}'}})-[i:INVOLVES]->(e) "
            f"WHERE i.role = '{Role.SUBJECT}' RETURN e.id",
            returns="eid agtype",
        )
        subjects = [uuid.UUID(unquote_agtype(eid)) for (eid,) in rows]
        cmap = await self._canonical_map(session, box)
        levels = {await self._ancestor_count(session, cmap.get(s, s)) for s in subjects}
        return sorted(levels)
