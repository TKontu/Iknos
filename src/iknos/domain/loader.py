"""Persist a validated domain pack into the AGE graph (G0.7; architecture.md §9).

A loaded pack is **one ``Box`` registry vertex** plus its part-whole taxonomy —
``Object`` vertices joined by ``directPartOf`` edges, with the derived ``partOf``
closure materialized (§10, §14). Everything is tagged with the pack's ``box`` id
so management and retrieval can be box-scoped (§9 "soft separation"). Packs are
**ingested once, read-only** (the Phase 1 §6.1 reference-amortization target,
``gap_phase_1_ingest.md`` G1.8).

Two robustness properties, both leaning on the deterministic ids in ``pack.py``:

- **Immutable per version (G0.R1).** A pack is identified by ``(name, version)``;
  re-loading identical content is a true no-op (no writes, so the bitemporal
  ``valid_from`` never moves), and re-loading *changed* content under the same
  version raises ``PackImmutabilityError`` rather than silently diverging. Writes
  are ``MERGE`` on id, so a load never duplicates. Re-activation, retries, and a
  re-run migration are all safe. See ``load_pack`` for the full branch table.
- **Atomic in the caller's transaction.** ``load_pack`` issues all writes on the
  passed session and does **not** commit; the caller owns the transaction
  boundary, so a single ``commit`` makes the whole pack appear at once (and any
  failure rolls the whole pack back). This is also what makes the no-op branch
  safe: a committed Box implies a committed (complete) pack.

**Activation seam.** Investigation-scoped activation (an investigation activates
the packs it needs, §9) arrives with the Task/investigation entity in Phase 6 —
most naturally an ``ACTIVATES`` edge from the root Task to the pack ``Box``. Until
then, ``Box.status == active`` is the activation flag and ``list_active_packs``
is the lookup.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.registry import deprecate_box
from iknos.boxes.serde import box_to_props
from iknos.db.age import execute_cypher, merge_edge, merge_vertex, unquote_agtype
from iknos.domain.pack import DomainPack
from iknos.provenance.action_log import record_action

# Box property marking a registry vertex as a domain pack — the reliable
# discriminator for active-pack queries (a plain Box has no `kind`).
PACK_KIND = "domain_pack"


@dataclass(frozen=True)
class LoadedPack:
    """Result of a load — the deterministic ids and counts written."""

    box_id: uuid.UUID
    entity_ids: dict[str, uuid.UUID]  # taxonomy key -> Object id
    direct_part_of: int  # directPartOf edges written
    part_of: int  # partOf edges written (direct + rollup closure)
    already_loaded: bool = field(default=False)


class PackImmutabilityError(Exception):
    """Raised when a pack is re-loaded with **changed content under the same
    ``(name, version)``** (G0.R1).

    A domain pack is immutable per version: the same ``(name, version)`` maps to
    the same deterministic Box id regardless of content, so silently re-writing
    would let the graph diverge from the declaration and would move the
    bitemporal ``valid_from``. Bump the version instead — a new version is a new
    Box and the old one deprecates rather than mutating.
    """


@dataclass(frozen=True)
class _BoxState:
    """The subset of an existing pack Box the loader needs to decide a re-load."""

    content_hash: str | None  # None = legacy Box written before G0.R1 hashing


async def _loaded_box_state(session: AsyncSession, box_id: uuid.UUID) -> _BoxState | None:
    """The stored state of this pack's Box, or ``None`` if it was never loaded.

    Reads back ``content_hash`` so ``load_pack`` can distinguish an identical
    re-load (no-op) from a changed-content re-load (immutability error) without
    rewriting anything.
    """
    rows = await execute_cypher(
        session,
        f"MATCH (b:Box {{id: '{box_id}'}}) RETURN b.content_hash",
        returns="content_hash agtype",
    )
    if not rows:
        return None
    raw = rows[0][0]
    return _BoxState(content_hash=None if raw is None else unquote_agtype(raw))


def _loaded_result(
    pack: DomainPack, entity_ids: dict[str, uuid.UUID], *, already_loaded: bool
) -> LoadedPack:
    """Build the ``LoadedPack`` return value purely from the pack (no DB reads).

    Counts are a function of the declaration, so a no-op re-load reports the same
    ids/counts a fresh load would, without re-querying the graph.
    """
    return LoadedPack(
        box_id=pack.box_id,
        entity_ids=entity_ids,
        direct_part_of=len(pack.part_of),
        part_of=len(pack.transitive_closure()),
        already_loaded=already_loaded,
    )


async def load_pack(
    session: AsyncSession,
    pack: DomainPack,
    *,
    valid_from: datetime | None = None,
) -> LoadedPack:
    """MERGE a pack's Box + taxonomy into the graph (caller commits).

    **Immutable per version.** A pack is identified by ``(name, version)`` (its
    deterministic Box id), and that identity is content-independent. So this is
    not a blind upsert:

    - **First load** (Box absent): write the Box + taxonomy, stamp ``valid_from``
      (now, or the passed value), and record ``content_hash``.
    - **Identical re-load** (same ``content_hash``): a true **no-op** — no writes
      are issued, so the bitemporal ``valid_from`` is preserved exactly. Returns
      ``already_loaded=True``. Safe for retries, re-activation, and a re-run
      migration (the original G0.R1 motivation).
    - **Changed content, same version**: raises :class:`PackImmutabilityError`
      rather than silently diverging — bump the version (a new Box).
    - **Legacy Box** (no stored ``content_hash``, e.g. a dev graph predating
      G0.R1): adopt the hash in place (one ``SET``), leaving ``valid_from``
      untouched; treated as a no-op otherwise.

    ``valid_from`` is therefore **create-only**: the moment this pack version
    became valid, never moved by a re-load. Atomicity makes the no-op-on-existing
    branch safe — ``load_pack`` issues no commit, so a committed Box implies a
    committed (complete) pack, with no half-written state to repair.
    """
    entity_ids = {e.key: pack.entity_id(e.key) for e in pack.entities}
    content_hash = pack.content_hash

    state = await _loaded_box_state(session, pack.box_id)
    if state is not None:
        # Box already present — never rewrite content or valid_from.
        if state.content_hash == content_hash:
            return _loaded_result(pack, entity_ids, already_loaded=True)
        if state.content_hash is None:
            # Legacy Box (pre-G0.R1): adopt the hash without touching valid_from.
            await execute_cypher(
                session,
                f"MATCH (b:Box {{id: '{pack.box_id}'}}) SET b.content_hash = '{content_hash}'",
            )
            return _loaded_result(pack, entity_ids, already_loaded=True)
        raise PackImmutabilityError(
            f"domain pack '{pack.name}@{pack.version}' was already loaded with different "
            f"content (stored hash {state.content_hash[:12]}…, declared {content_hash[:12]}…). "
            f"A pack version is immutable — bump the version to change it."
        )

    # --- first load: stamp valid_from once and persist content_hash ---
    stamp = valid_from or datetime.now(UTC)
    when = stamp.isoformat()
    box = pack.to_box(stamp)

    # --- Box registry vertex: shared core serialization (box_to_props) + pack extras ---
    # The core Box properties go through the same contract the box registry uses, so the
    # pack and general box-write paths cannot diverge (the G0.R1 divergence class). The
    # pack-only metadata below is *not* on the domain-agnostic Box model (§9, §10).
    extra: dict[str, Any] = {
        "kind": PACK_KIND,
        # Content hash anchoring per-version immutability (G0.R1, see load_pack).
        "content_hash": content_hash,
        # Entity-type ontology travels with the Box so active-pack consumers
        # (entity linking, Phase 1) read legal types from the graph, not a file.
        # Forward path: promote to first-class type nodes if they need edges.
        "entity_types": [t.model_dump(exclude_none=True) for t in pack.entity_types],
    }
    if pack.description is not None:
        extra["description"] = pack.description
    await merge_vertex(session, "Box", box_to_props(box, extra=extra))
    # Box creation is an auditable lifecycle event (§10.1), uniform with the registry's
    # create-box Action — emitted only on a real first load (this branch).
    await record_action(
        session,
        actor="pack-loader",
        action_type="create-box",
        inputs={"name": box.name, "tier": str(box.tier), "version": box.version},
        outputs={"box": str(box.id)},
    )

    # --- taxonomy entities as Object vertices, box-tagged ---
    for e in pack.entities:
        await merge_vertex(
            session,
            "Object",
            {
                "id": str(entity_ids[e.key]),
                "box": str(box.id),
                "label": e.label,
                "type": e.type,
            },
        )

    # --- directPartOf edges (the declared steps; anchored provenance, §14) ---
    for rel in pack.part_of:
        await merge_edge(
            session,
            src_id=entity_ids[rel.part],
            dst_id=entity_ids[rel.whole],
            label="directPartOf",
            props={
                "box": str(box.id),
                "meronymy_type": str(rel.meronymy),
                "anchored": True,  # from the pack taxonomy, not text-induced (§14)
                "valid_from": when,
            },
        )

    # --- partOf closure (derived; materialized for query, recompute on change) ---
    closure = pack.transitive_closure()
    for edge in closure:
        await merge_edge(
            session,
            src_id=entity_ids[edge.part],
            dst_id=entity_ids[edge.whole],
            label="partOf",
            props={
                "box": str(box.id),
                "meronymy_type": str(edge.meronymy),
                "derivation": edge.derivation,
                "anchored": True,
                "valid_from": when,
            },
        )

    return _loaded_result(pack, entity_ids, already_loaded=False)


async def list_active_packs(session: AsyncSession) -> list[dict[str, str]]:
    """Active domain-pack Boxes — the current (pre-Phase-6) activation lookup."""
    rows = await execute_cypher(
        session,
        f"MATCH (b:Box {{kind: '{PACK_KIND}', status: 'active'}}) RETURN b.id, b.name, b.version",
        returns="id agtype, name agtype, version agtype",
    )
    return [
        {
            "id": unquote_agtype(r[0]),
            "name": unquote_agtype(r[1]),
            "version": unquote_agtype(r[2]),
        }
        for r in rows
    ]


async def deprecate_pack(
    session: AsyncSession, box_id: uuid.UUID, *, valid_to: datetime | None = None
) -> None:
    """Flip a pack Box to deprecated (§9). Belief revision on dependents is the
    governance track's job; here we only close the box (caller commits).

    Delegates to the box registry so pack and general boxes deprecate through one path
    (status + ``valid_to`` close, plus the ``deprecate-box`` Action)."""
    await deprecate_box(session, box_id, valid_to=valid_to, actor="pack-loader")
