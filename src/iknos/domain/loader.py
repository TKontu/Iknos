"""Persist a validated domain pack into the AGE graph (G0.7; architecture.md §9).

A loaded pack is **one ``Box`` registry vertex** plus its part-whole taxonomy —
``Object`` vertices joined by ``directPartOf`` edges, with the derived ``partOf``
closure materialized (§10, §14). Everything is tagged with the pack's ``box`` id
so management and retrieval can be box-scoped (§9 "soft separation"). Packs are
**ingested once, read-only** (the Phase 1 §6.1 reference-amortization target,
``gap_phase_1_ingest.md`` G1.8).

Two robustness properties, both leaning on the deterministic ids in ``pack.py``:

- **Idempotent.** Ids are derived from ``(name, version, key)``, and every write
  is a ``MERGE`` on id, so re-loading a pack is a no-op rather than a duplicate —
  re-activation, retries, and a re-run migration are all safe.
- **Atomic in the caller's transaction.** ``load_pack`` issues all writes on the
  passed session and does **not** commit; the caller owns the transaction
  boundary, so a single ``commit`` makes the whole pack appear at once (and any
  failure rolls the whole pack back).

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

from iknos.db.age import cypher_map, execute_cypher
from iknos.domain.pack import DomainPack

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


async def is_pack_loaded(session: AsyncSession, pack: DomainPack) -> bool:
    """True if a Box with this pack's deterministic id already exists."""
    rows = await execute_cypher(
        session,
        f"MATCH (b:Box {{id: '{pack.box_id}'}}) RETURN count(b)",
        returns="n agtype",
    )
    return bool(rows) and int(rows[0][0]) > 0


async def load_pack(
    session: AsyncSession,
    pack: DomainPack,
    *,
    valid_from: datetime | None = None,
) -> LoadedPack:
    """MERGE a pack's Box + taxonomy into the graph (caller commits).

    Idempotent: a second call with the same pack rewrites identical properties
    and creates no duplicates. Returns the deterministic ids and the edge counts.
    """
    stamp = valid_from or datetime.now(UTC)
    when = stamp.isoformat()
    box = pack.to_box(stamp)
    entity_ids = {e.key: pack.entity_id(e.key) for e in pack.entities}

    already = await is_pack_loaded(session, pack)

    # --- Box registry vertex: core Box props + pack metadata (extra AGE props) ---
    box_props: dict[str, Any] = {
        "id": str(box.id),
        "name": box.name,
        "tier": str(box.tier),
        "version": box.version,
        "source": box.source,
        "reliability_prior": box.reliability_prior,
        "valid_from": when,
        "valid_to": None,
        "status": str(box.status),
        # Pack-layer metadata — not on the domain-agnostic core Box model (§9, §10).
        "kind": PACK_KIND,
        # Entity-type ontology travels with the Box so active-pack consumers
        # (entity linking, Phase 1) read legal types from the graph, not a file.
        # Forward path: promote to first-class type nodes if they need edges.
        "entity_types": [t.model_dump(exclude_none=True) for t in pack.entity_types],
    }
    if pack.description is not None:
        box_props["description"] = pack.description
    await _merge_node(session, "Box", box_props)

    # --- taxonomy entities as Object vertices, box-tagged ---
    for e in pack.entities:
        await _merge_node(
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
        await _merge_edge(
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
        await _merge_edge(
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

    return LoadedPack(
        box_id=box.id,
        entity_ids=entity_ids,
        direct_part_of=len(pack.part_of),
        part_of=len(closure),
        already_loaded=already,
    )


async def list_active_packs(session: AsyncSession) -> list[dict[str, str]]:
    """Active domain-pack Boxes — the current (pre-Phase-6) activation lookup."""
    rows = await execute_cypher(
        session,
        f"MATCH (b:Box {{kind: '{PACK_KIND}', status: 'active'}}) RETURN b.id, b.name, b.version",
        returns="id agtype, name agtype, version agtype",
    )
    return [{"id": _unquote(r[0]), "name": _unquote(r[1]), "version": _unquote(r[2])} for r in rows]


async def deprecate_pack(
    session: AsyncSession, box_id: uuid.UUID, *, valid_to: datetime | None = None
) -> None:
    """Flip a pack Box to deprecated (§9). Belief revision on dependents is the
    governance track's job; here we only close the box (caller commits)."""
    when = (valid_to or datetime.now(UTC)).isoformat()
    await execute_cypher(
        session,
        f"MATCH (b:Box {{id: '{box_id}'}}) SET b.status = 'deprecated', b.valid_to = '{when}'",
    )


# --- AGE helpers (MERGE-on-id keeps loads idempotent) ---


async def _merge_node(session: AsyncSession, label: str, props: dict[str, Any]) -> None:
    """``MERGE (n:Label {id}) SET n = {...}`` — upsert keyed on id."""
    body = cypher_map(props)
    await execute_cypher(
        session,
        f"MERGE (n:{label} {{id: '{props['id']}'}}) SET n = {body}",
    )


async def _merge_edge(
    session: AsyncSession,
    *,
    src_id: uuid.UUID,
    dst_id: uuid.UUID,
    label: str,
    props: dict[str, Any],
) -> None:
    """MERGE one edge of ``label`` between two id-identified nodes, then set props.

    The pack guarantees at most one edge of a given label per (part, whole) pair,
    so merging on endpoints+label (not on properties) is the correct idempotent key.
    """
    body = cypher_map(props)
    await execute_cypher(
        session,
        f"MATCH (a {{id: '{src_id}'}}), (b {{id: '{dst_id}'}}) "
        f"MERGE (a)-[r:{label}]->(b) SET r = {body}",
    )


def _unquote(v: Any) -> str:
    """AGE returns agtype strings double-quoted (``\"foo\"``); strip to plain str."""
    s = str(v)
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s
