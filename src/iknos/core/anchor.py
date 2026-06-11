"""The entity-linking / taxonomy-anchoring subsystem (Phase 2, G2.8; architecture.md §5.2,
§9, §14, §6, §10).

A case box's ``Actor``/``Object`` entities (G2.2 emits one fresh node per mention) denote
real-world things that often already have a canonical identity in the **active domain
pack(s)' taxonomy** — the curated ``Object`` nodes a pack loads (``domain/loader``). Linking
a case entity to its taxonomy node is **anchoring**, the *primary, reliable* identity/level
path (§14 "anchor first"; §9): the taxonomy is authoritative, so an anchor gives the entity a
high-confidence canonical identity and a real partonomy depth — where text-induced meronymy
(G2.5) is the domain-fragile fallback. Anchoring is "the reliability driver across domains"
(§ phase risks): a domain works well exactly when its pack's taxonomy covers most referents.

The link is a directed, scored ``ANCHORS_TO`` edge (case entity → taxonomy node). The
direction encodes **anchor canonicalizes** (§5.2/§14): the taxonomy node is the authoritative
identity an anchored entity takes on — *not* a peer ``SAME_AS`` whose canonical is merely the
min-id of a within-box component. Anchoring crosses boxes (case → reference pack), which is
why it is its own edge rather than an overloaded ``SAME_AS`` (§9: cross-box identity is not the
within-box resolution component).

The default is **conservative** (the ``resolve``/``reference`` precedent): an anchor is
``CONFIRMED`` only when a single taxonomy node clears a high bar — in practice an exact
normalized-label match within the active pack scope; otherwise it stays **open** (one or more
``CANDIDATE`` edges) for expert disambiguation. An over-eager anchor mis-canonicalizes an
entity and corrupts its derived level, so an open anchor is the safer failure.

Pure/DB split (the ``core/resolve.py`` discipline): the cascade — ``block_anchors``,
``score_anchor``, ``decide_anchor``, ``anchors_to_props`` — and the coverage math are DB-free
and unit-testable; ``iknos.db.age`` is imported lazily inside the ``EntityLinker`` DB methods
so importing this module never pulls in the ``DATABASE_URL`` config singleton. Linking is
**deterministic and lexical** — no LLM and no embeddings in the path (§5.2: similarity is a
blocking signal only; §14: not embedding cosine / lexical concreteness). Re-anchoring an
unchanged (box, taxonomy) recomputes the same edge set; ``ANCHORS_TO`` is written through the
upsert ``merge_edge`` (keyed on endpoints + label), so a re-run writes no duplicates.

Scope deliberately left to later slices (documented seams):

- **Anchor-canonicalization fold** — making ``resolve.canonical_components`` and the
  ``partwhole`` level read prefer an entity's confirmed anchor as its canonical identity /
  level source (§5.2/§14) → G2.8 slice 2. This slice writes the ``ANCHORS_TO`` edges and
  exposes the reads (:meth:`anchored_targets`, :meth:`coverage`) those consumers will use; it
  does **not** yet mutate the shipped ``resolve``/``partwhole`` behaviour.
- **Taxonomy-anchor stage in the reference binder** (§3.1 cascade tail) — a ``Mention`` that
  fails the in-graph stage binds to a taxonomy node via the same linking → with slice 2.
- **Embedding-neighbourhood blocking** (§5.2) — needs an entity-embedding store; this slice
  blocks on shared normalized tokens only.
- **Belief-revision / retraction** of a stale anchor when the taxonomy or the entity changes
  (re-run Layer A/B; clean superseded ``ANCHORS_TO``) → Phase 3. This slice's edge set is
  monotonic per run.
- **Investigation-scoped pack activation** (§9) — this slice anchors against *all* active
  packs (``loader.list_active_packs``); the ``ACTIVATES``-edge scoping arrives with the Task
  entity (Phase 6). The ``pack_box_ids`` parameter is the seam where that scope plugs in.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.extract import NodeKind
from iknos.core.reference import Referent, group_referents
from iknos.provenance.action_log import record_action
from iknos.types.edges import AnchorState

# Note: iknos.db.age and iknos.domain.loader are imported lazily inside the EntityLinker DB
# methods (see module docstring), so importing this module stays DB-free for the unit tests of
# the cascade.

# Bump on any change to the linking pipeline (the cascade weights/bars, the blocking or
# scoring logic). Stored on each anchor Action so an ANCHORS_TO edge's producing pipeline is
# identifiable (mirrors resolve.RESOLVE_SCHEMA_VERSION).
ANCHOR_SCHEMA_VERSION = 1

# Decision bars (§5.2 conservative default, mirroring resolve/reference). CONFIRM is
# deliberately high — an over-eager anchor mis-canonicalizes; the band [CANDIDATE, CONFIRM)
# records an open, bridgeable anchor without committing the identity. TIE_MARGIN keeps
# near-tied taxonomy nodes all as candidates (an ambiguous cross-pack homonym).
ANCHOR_CONFIRM_BAR = 0.85
ANCHOR_CANDIDATE_BAR = 0.50
ANCHOR_TIE_MARGIN = 0.05

# Scoring weights. An **exact** normalized-label match to a curated taxonomy node is the
# reliable anchor signal (the taxonomy is a controlled vocabulary), so it must reach the
# confirm bar on its own; token *containment* (a case surface that is a shorter/longer form of
# the taxonomy label, "pump" ~ "Centrifugal pump") is the partial signal that lands in the
# candidate band — never an auto-confirm. Type agreement is a faint tie-breaker only: the case
# entity's ``type`` is a free-text LLM guess while the taxonomy's is the pack's ``EntityType``
# name, so they rarely string-match and a mismatch must **not** disconfirm (unlike resolve,
# where both sides are LLM-typed).
_W_CONTAIN = 0.55
_W_EXACT = 0.40
_W_TYPE = 0.05


@dataclass(frozen=True)
class TaxonomyNode:
    """One domain-pack taxonomy ``Object`` — a candidate anchor target (§9, §14).

    Loaded from an active pack Box; ``box`` is the pack's Box id (carried onto the edge so an
    anchor's source pack is auditable without a second hop). Single-kind by construction (a
    pack's part-whole taxonomy is ``Object`` nodes), so — unlike ``EntityRecord`` — it carries
    no ``NodeKind``: blocking is lexical only, and a case ``Actor`` mis-classified by the
    extractor can still anchor to the right ``Object`` rather than being kind-gated out.
    """

    id: uuid.UUID
    box: uuid.UUID
    label: str
    type: str

    @property
    def norm(self) -> str:
        from iknos.core.resolve import normalize_label

        return normalize_label(self.label)

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self.norm.split())


def block_anchors(entity: Referent, nodes: list[TaxonomyNode]) -> list[TaxonomyNode]:
    """The cheap blocking stage: taxonomy nodes sharing ≥1 normalized token with ``entity``.

    Lexical only (no kind gate — the taxonomy is single-kind, §9). An entity whose normalized
    label is empty (punctuation-only surface) shares no token and blocks to the empty set.

    *Seam:* embedding-neighbourhood blocking (§5.2) is deferred — needs an entity-embedding
    store; this stage blocks on shared tokens only.
    """
    et = entity.tokens
    if not et:
        return []
    return [n for n in nodes if et & n.tokens]


def score_anchor(entity: Referent, node: TaxonomyNode) -> float:
    """Deterministic anchor score in [0, 1] from lexical + attribute evidence (§5.2, §14).

    Signals (similarity/embeddings are **not** signals — anchoring is lexical, §14):

    - **Containment.** The best of the two token-containment fractions (the case surface
      covered by the taxonomy label, or vice versa) — a referring surface is often a
      shorter/longer form of the canonical taxonomy name ("pump" ~ "Centrifugal pump").
    - **Exact label.** A strong bonus when the normalized surfaces are identical — the
      controlled-vocabulary anchor signal that, alone, reaches the confirm bar.
    - **Type agreement.** A faint bonus when the (noisy, free-text vs pack-ontology) types
      string-match; never disconfirming on a mismatch.

    Weighted so an exact label reaches the confirm bar while mere containment lands in the
    candidate band — partial evidence never auto-confirms (the conservative default).
    """
    et, nt = entity.tokens, node.tokens
    shared = et & nt
    if not shared:
        return 0.0
    containment = max(len(shared) / len(et), len(shared) / len(nt))
    exact = 1.0 if entity.norm and entity.norm == node.norm else 0.0
    type_agree = 1.0 if entity.type and node.type and entity.type == node.type else 0.0

    score = _W_CONTAIN * containment + _W_EXACT * exact + _W_TYPE * type_agree
    return max(0.0, min(1.0, score))


@dataclass(frozen=True)
class AnchorDecision:
    """The cascade's verdict for one case entity: the committed/open state + chosen targets.

    ``state`` is ``CONFIRMED`` (one ``targets`` entry — the identity is committed),
    ``CANDIDATE`` (one or more open competing targets), or ``None`` (unresolved — no taxonomy
    node cleared the candidate bar, ``targets`` empty). ``score`` is the top score (0.0 when
    unresolved). ``anchored`` is true only for ``CONFIRMED`` — the single signal the coverage
    metric and slice-2 canonicalization use.
    """

    state: AnchorState | None
    targets: list[TaxonomyNode]
    score: float

    @property
    def anchored(self) -> bool:
        return self.state is AnchorState.CONFIRMED


def decide_anchor(entity: Referent, nodes: list[TaxonomyNode]) -> AnchorDecision:
    """Map the scored taxonomy candidates to an anchor decision (§5.2 conservative default).

    Scores every blocked node, keeps those at/above the candidate bar, and ranks them
    deterministically (score desc, then id). With a single top node at/above the confirm bar
    and no near-tie, the anchor is ``CONFIRMED`` to it; otherwise — a near-tie (a cross-pack
    homonym) or a top in the candidate band — the decision is ``CANDIDATE`` over the tied set,
    kept open (§ phase risks: cross-domain ambiguity is resolved by pack scope + expert review).
    Empty when nothing clears the candidate bar.
    """
    scored = sorted(
        ((n, score_anchor(entity, n)) for n in nodes),
        key=lambda ns: (-ns[1], str(ns[0].id)),
    )
    scored = [(n, s) for n, s in scored if s >= ANCHOR_CANDIDATE_BAR]
    if not scored:
        return AnchorDecision(state=None, targets=[], score=0.0)

    top = scored[0][1]
    tied = [n for n, s in scored if top - s <= ANCHOR_TIE_MARGIN]
    if top >= ANCHOR_CONFIRM_BAR and len(tied) == 1:
        return AnchorDecision(state=AnchorState.CONFIRMED, targets=[tied[0]], score=top)
    return AnchorDecision(state=AnchorState.CANDIDATE, targets=tied, score=top)


def anchors_to_props(
    *,
    box: uuid.UUID,
    target_box: uuid.UUID,
    state: AnchorState,
    strength: float,
    now: datetime,
) -> dict[str, Any]:
    """Flatten an ``ANCHORS_TO`` edge to AGE properties — the canonical write contract.

    The single place ``ANCHORS_TO`` serialization lives (cf. ``resolve.same_as_to_props``).
    ``box`` is the **case** box the anchor assertion belongs to (§9); ``target_box`` is the
    pack box the taxonomy node lives in (the anchored-to pack, recorded for audit/scope).
    ``strength`` is the calibrated link score (§8/§10); the **two §12 annotations** are seeded
    — ``support_count = 1`` (this one linking act grounds the edge) and ``confidence`` from the
    strength (the Layer-B value is recomputed by Phase 3 when re-anchoring becomes belief
    revision). Bitemporal fields are stamped open (``valid_to``/``event_time`` null).
    """
    return {
        "box": str(box),
        "target_box": str(target_box),
        "state": str(state),
        "strength": strength,
        "support_count": 1,
        "confidence": strength,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


@dataclass(frozen=True)
class AnchorEdge:
    """One written anchor edge: a case entity (canonical id) → a taxonomy node, with state."""

    src: uuid.UUID
    dst: uuid.UUID
    state: AnchorState
    strength: float


@dataclass(frozen=True)
class AnchorCoverage:
    """The §14 coverage-policy metric: the fraction of a box's entities that **confirm**-anchor.

    ``total`` is the canonical case entities considered (label-grouped ``Actor``/``Object``);
    ``anchored`` is how many carry a ``CONFIRMED`` ``ANCHORS_TO``. High coverage means anchoring
    is the level/identity mechanism for the domain; persistently low coverage means the pack's
    taxonomy is inadequate — escalate to induction + review and mark levels provisional (§14).
    ``fraction`` is ``anchored / total`` (0.0 for an empty box).
    """

    total: int
    anchored: int

    @property
    def fraction(self) -> float:
        return self.anchored / self.total if self.total else 0.0


@dataclass(frozen=True)
class AnchorResult:
    """The outcome of anchoring one box: the Action id, the edges written by state, and the
    coverage achieved (computed from the run, no extra query)."""

    action_id: uuid.UUID
    confirmed: list[AnchorEdge]
    candidate: list[AnchorEdge]
    coverage: AnchorCoverage


class EntityLinker:
    """The entity-linking operator (§6): case ``Actor``/``Object`` → ``ANCHORS_TO`` taxonomy.

    DB-free to construct; the cascade is pure and deterministic (no LLM), and the graph writes
    happen in the DB methods. Stateless across calls. Re-running on an unchanged box +
    taxonomy recomputes the same edge set, written through the upsert ``merge_edge``, so a
    re-run writes no duplicates (the ``resolve`` structural-idempotency discipline — the anchor
    Action is an audit record per run, not an idempotency key).
    """

    async def _active_pack_box_ids(self, session: AsyncSession) -> list[uuid.UUID]:
        """The active domain packs' Box ids — the taxonomy scope to anchor against (§9).

        The current (pre-Phase-6) activation lookup; investigation-scoped activation arrives
        with the Task entity (the ``pack_box_ids`` parameter on :meth:`anchor_box` is the seam).
        """
        from iknos.domain.loader import list_active_packs

        return [uuid.UUID(p["id"]) for p in await list_active_packs(session)]

    async def _load_taxonomy(
        self, session: AsyncSession, pack_box_ids: list[uuid.UUID]
    ) -> list[TaxonomyNode]:
        """Load the candidate anchor targets: the ``Object`` nodes of the given pack Boxes.

        One query per pack box keeps the Cypher simple and rides the ``Object`` property GIN
        (0007) on the ``box`` containment filter; the taxonomy is small (a pack's curated
        entities), so the per-box round-trips are cheap.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        nodes: list[TaxonomyNode] = []
        for pack_box in pack_box_ids:
            bx = str(pack_box)
            rows = await execute_cypher(
                session,
                f"MATCH (o:Object {{box: '{bx}'}}) RETURN o.id, o.label, o.type",
                returns="oid agtype, lab agtype, typ agtype",
            )
            for oid, lab, typ in rows:
                nodes.append(
                    TaxonomyNode(
                        id=uuid.UUID(unquote_agtype(oid)),
                        box=pack_box,
                        label=unquote_agtype(lab),
                        type=unquote_agtype(typ),
                    )
                )
        return nodes

    async def _load_case_entities(self, session: AsyncSession, box: uuid.UUID) -> list[Referent]:
        """Load a case box's ``Actor``/``Object`` nodes as canonical referents.

        Reuses ``reference.group_referents`` (same-label fresh nodes collapse to one canonical
        id), so an anchor connects the **canonical** case entity — the same representative the
        ``partwhole`` level read and ``resolve`` component pick — to the taxonomy node, robust
        to whether entity resolution (G2.3) has run.
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

    async def _persist_anchor(
        self,
        session: AsyncSession,
        entity: Referent,
        node: TaxonomyNode,
        state: AnchorState,
        strength: float,
        box: uuid.UUID,
        now: datetime,
    ) -> AnchorEdge:
        """Write one ``ANCHORS_TO`` edge: case entity (canonical) → taxonomy node.

        Directed case → taxonomy (anchor canonicalizes — direction is semantic, not the
        ``SAME_AS`` min-id canonical). ``merge_edge`` keys on (src, dst, label), so a re-run
        upserts rather than duplicating.
        """
        from iknos.db.age import merge_edge

        await merge_edge(
            session,
            src_id=entity.canonical,
            dst_id=node.id,
            label="ANCHORS_TO",
            props=anchors_to_props(
                box=box, target_box=node.box, state=state, strength=strength, now=now
            ),
        )
        return AnchorEdge(src=entity.canonical, dst=node.id, state=state, strength=strength)

    async def anchor_box(
        self,
        session: AsyncSession,
        box: uuid.UUID,
        *,
        pack_box_ids: list[uuid.UUID] | None = None,
    ) -> AnchorResult:
        """Anchor one case box to the active taxonomy: load → block → score → decide → persist.

        The §6 operator shape, box-scoped. ``pack_box_ids`` overrides the scope (the
        investigation-activation seam); when ``None`` it anchors against all active packs.
        Emits one ``anchor`` Action (``actor="entity-linker"``) naming the entities consulted
        and the edges written by state (§10.1), then commits. Returns the edges and the
        coverage achieved.
        """
        now = datetime.now(UTC)
        if pack_box_ids is None:
            pack_box_ids = await self._active_pack_box_ids(session)
        taxonomy = await self._load_taxonomy(session, pack_box_ids)
        entities = await self._load_case_entities(session, box)

        confirmed: list[AnchorEdge] = []
        candidate: list[AnchorEdge] = []
        anchored_entities = 0
        for entity in entities:
            decision = decide_anchor(entity, block_anchors(entity, taxonomy))
            if decision.state is None:
                continue
            if decision.anchored:
                anchored_entities += 1
            for node in decision.targets:
                edge = await self._persist_anchor(
                    session, entity, node, decision.state, decision.score, box, now
                )
                (confirmed if decision.anchored else candidate).append(edge)

        action_id = await record_action(
            session,
            actor="entity-linker",
            action_type="anchor",
            inputs={
                "box": str(box),
                "packs": [str(p) for p in pack_box_ids],
                "entities": [str(e.canonical) for e in entities],
                "schema_version": ANCHOR_SCHEMA_VERSION,
            },
            outputs={
                "confirmed": [f"{e.src}->{e.dst}" for e in confirmed],
                "candidate": [f"{e.src}->{e.dst}" for e in candidate],
            },
        )
        await session.commit()
        return AnchorResult(
            action_id=action_id,
            confirmed=confirmed,
            candidate=candidate,
            coverage=AnchorCoverage(total=len(entities), anchored=anchored_entities),
        )

    async def anchored_targets(
        self, session: AsyncSession, box: uuid.UUID
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Read the box's **confirmed** anchors: canonical case entity id → taxonomy node id.

        The anchor-canonicalization read slice-2 consumers (``resolve`` identity, ``partwhole``
        level) use to take the taxonomy node as an entity's canonical identity / level source
        (§5.2/§14). ``CANDIDATE`` anchors are excluded — they keep the identity open by
        definition. A confirmed anchor is single-target, so the map is well-defined.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        bx = str(box)
        rows = await execute_cypher(
            session,
            f"MATCH (e {{box: '{bx}'}})-[r:ANCHORS_TO]->(t) "
            f"WHERE r.state = '{AnchorState.CONFIRMED}' RETURN e.id, t.id",
            returns="eid agtype, tid agtype",
        )
        return {uuid.UUID(unquote_agtype(eid)): uuid.UUID(unquote_agtype(tid)) for eid, tid in rows}

    async def coverage(self, session: AsyncSession, box: uuid.UUID) -> AnchorCoverage:
        """Anchoring coverage for a box (§14): confirmed-anchored / total canonical entities.

        A standalone read (re-derivable any time, unlike the run-time :attr:`AnchorResult.coverage`)
        — the Trial-A4 anchoring-coverage measurement and the policy signal for whether a pack
        is adequate for level attachment (§14).
        """
        entities = await self._load_case_entities(session, box)
        anchored = await self.anchored_targets(session, box)
        present = {e.canonical for e in entities} & anchored.keys()
        return AnchorCoverage(total=len(entities), anchored=len(present))
