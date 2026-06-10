"""Box registry — create, read, scope, deprecate boxes in AGE (G2.1; §9, §10.1).

A box is **one ``(:Box)`` registry vertex** in the single AGE graph (§9 "soft
separation": boxes are a logical partition by a ``box`` property, not separate graphs).
This module is the management surface over those vertices; the domain-agnostic
serialization it relies on lives in ``serde.py`` (shared with the pack loader).

Two disciplines, both inherited from the G0.R1 lesson:

- **Create-only ``valid_from``.** :func:`create_box` reads first and, when the box
  already exists, returns the **stored** box without writing — so the bitemporal anchor
  is never moved by a re-create. Box *metadata editing* (changing reliability, source)
  is a separate, later concern (governance / soft override); a re-create is a no-op,
  not an update.
- **Caller owns the transaction.** Like ``load_pack``, the write functions issue their
  statements on the passed session and do **not** commit; the caller's single commit
  makes the box (and its ``Action``) appear atomically.

Every box lifecycle event appends an :class:`~iknos.db.orm.Action` (§10.1) — auditability
is present from creation, not retrofitted.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_from_props, box_to_props
from iknos.provenance.action_log import record_action
from iknos.types.nodes import Box, BoxStatus, Tier

# iknos.db.age is imported lazily inside each function (not at module top) so that
# importing this module — or the boxes package, which re-exports it — does not pull in
# the config singleton (DATABASE_URL). This keeps the pure serde layer and the box
# constructors importable in DB-free unit tests. Same discipline as core/proposition.py.

# Action actor for registry-driven box lifecycle (distinct from "pack-loader").
REGISTRY_ACTOR = "box-registry"


async def get_box(session: AsyncSession, box_id: uuid.UUID) -> Box | None:
    """Read one box back as a :class:`Box`, or ``None`` if it does not exist.

    Reads the whole property map in one round-trip and rebuilds via ``box_from_props``;
    works for both registry boxes and domain-pack boxes (pack-only extras are ignored).
    """
    from iknos.db.age import execute_cypher, parse_agtype_map

    rows = await execute_cypher(
        session,
        f"MATCH (b:Box {{id: '{box_id}'}}) RETURN properties(b)",
        returns="props agtype",
    )
    if not rows:
        return None
    return box_from_props(parse_agtype_map(rows[0][0]))


async def create_box(session: AsyncSession, box: Box, *, actor: str = REGISTRY_ACTOR) -> Box:
    """Create a box if absent; a re-create with the same id is a true no-op (§9).

    Returns the persisted box: the freshly-written one on first create, or the
    **existing** one on a re-create — so a caller that passed a now-stamped ``valid_from``
    (e.g. from :func:`~iknos.boxes.serde.case_box`) gets back the original anchor, never a
    moved one. Emits a ``create-box`` Action only when a box is actually written. Caller
    commits.
    """
    from iknos.db.age import merge_vertex

    existing = await get_box(session, box.id)
    if existing is not None:
        return existing
    await merge_vertex(session, "Box", box_to_props(box))
    await record_action(
        session,
        actor=actor,
        action_type="create-box",
        inputs={"name": box.name, "tier": str(box.tier), "version": box.version},
        outputs={"box": str(box.id)},
    )
    return box


async def list_boxes(
    session: AsyncSession,
    *,
    tier: Tier | None = None,
    status: BoxStatus | None = BoxStatus.ACTIVE,
) -> list[Box]:
    """All boxes, optionally filtered by ``tier`` and ``status`` (defaults to active).

    ``status=None`` returns every status. Ordered by ``reliability_prior`` descending so
    the most entrenched sources come first — the ordering reasoning consumes (§9).
    """
    from iknos.db.age import execute_cypher, parse_agtype_map

    clauses: list[str] = []
    if tier is not None:
        clauses.append(f"b.tier = '{tier}'")
    if status is not None:
        clauses.append(f"b.status = '{status}'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await execute_cypher(
        session,
        f"MATCH (b:Box) {where} RETURN properties(b) ORDER BY b.reliability_prior DESC",
        returns="props agtype",
    )
    return [box_from_props(parse_agtype_map(r[0])) for r in rows]


async def active_boxes_by_tier(session: AsyncSession, tiers: list[Tier]) -> list[Box]:
    """Active boxes in any of ``tiers``, ordered by ``reliability_prior`` desc (§9).

    The "reasoning reads across active boxes by tier + reliability" query — e.g. an
    investigation's working set spans several reference tiers plus its case tier.
    """
    from iknos.db.age import execute_cypher, parse_agtype_map

    if not tiers:
        return []
    tier_list = ", ".join(f"'{t}'" for t in tiers)
    rows = await execute_cypher(
        session,
        f"MATCH (b:Box) WHERE b.status = 'active' AND b.tier IN [{tier_list}] "
        "RETURN properties(b) ORDER BY b.reliability_prior DESC",
        returns="props agtype",
    )
    return [box_from_props(parse_agtype_map(r[0])) for r in rows]


async def deprecate_box(
    session: AsyncSession,
    box_id: uuid.UUID,
    *,
    valid_to: datetime | None = None,
    actor: str = REGISTRY_ACTOR,
) -> None:
    """Flip a box to ``deprecated`` and close its validity window (§9). Caller commits.

    Belief revision on everything derived from the box's facts is the non-monotonic
    layer's job (Phase 5); here we only close the box and log the event. ``SET`` of two
    scalars (not a full-replace) leaves ``valid_from`` and all other properties intact.
    """
    from iknos.db.age import execute_cypher

    when = (valid_to or datetime.now(UTC)).isoformat()
    await execute_cypher(
        session,
        f"MATCH (b:Box {{id: '{box_id}'}}) "
        f"SET b.status = '{BoxStatus.DEPRECATED}', b.valid_to = '{when}'",
    )
    await record_action(
        session,
        actor=actor,
        action_type="deprecate-box",
        inputs={"box": str(box_id)},
        outputs={"box": str(box_id), "valid_to": when},
    )
