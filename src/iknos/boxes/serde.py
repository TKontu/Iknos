"""Box serialization + pure constructors (Phase 2 G2.1; architecture.md §9, §10).

The **box** is the lifecycle/provenance unit (§9): every reasoning node and edge
carries a ``box`` id, and management operations are box-scoped. This module owns the
**single, canonical mapping** between the domain-agnostic :class:`~iknos.types.nodes.Box`
model and its flat AGE vertex properties — the one contract that the box registry, the
domain-pack loader, the dense/sparse indexes (G1.11), and later operators all read and
write through, so the serialization can never drift across call sites (the divergence
that produced the G0.R1 ``valid_from`` bug).

Pure module: no database or config import, so the property contract and the box
constructors are unit-testable without a live graph (and importing them never pulls in
the ``DATABASE_URL`` singleton). The DB-touching registry lives in ``registry.py``.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from iknos.types.governance import SourceInterest
from iknos.types.nodes import Box, BoxStatus, Tier

# Stable namespace for deterministically-derived box ids (cf. the pack namespace in
# ``domain/pack.py``). Deriving a box id from a stable key (name@version) makes box
# creation **idempotent**: re-ingesting a document into "its" case box recomputes the
# same id and MERGE-on-id no-ops instead of creating a duplicate box. Never change this
# value — it would orphan every box created under it. Domain packs keep their own
# namespace (their entity ids derive from the pack box id), so this is for the general
# (case/working) boxes the registry creates, not for packs.
_BOX_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "box.iknos")

# The core Box fields persisted as flat scalar properties. ``interest`` is handled
# separately (it flattens to interest_role/interest_stake, or is omitted when None).
_SCALAR_FIELDS = ("name", "version", "source", "reliability_prior")


def box_to_props(box: Box, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a :class:`Box` to AGE vertex properties (the canonical write contract).

    ``valid_from``/``valid_to`` serialize to ISO-8601 (``valid_to`` null while the box is
    open); ``tier``/``status`` to their enum string values; ``interest`` to flat
    ``interest_role``/``interest_stake`` **only when present** — an absent interest omits
    both keys, preserving the unknown (``None``) vs known-empty (``SourceInterest()``)
    distinction on read. ``extra`` carries non-core properties a specific writer adds
    (the pack loader's ``kind``/``content_hash``/``entity_types``); it is merged last.
    """
    props: dict[str, Any] = {
        "id": str(box.id),
        "tier": str(box.tier),
        "status": str(box.status),
        "valid_from": box.valid_from.isoformat(),
        "valid_to": box.valid_to.isoformat() if box.valid_to is not None else None,
    }
    for f in _SCALAR_FIELDS:
        props[f] = getattr(box, f)
    if box.interest is not None:
        props.update(box.interest.flatten())
    if extra:
        props.update(extra)
    return props


def _as_str_list(v: Any) -> list[str]:
    """A list-valued property may arrive as a real list (pure round-trip) or as a
    JSON string (read back from AGE, where cypher_map JSON-encoded it). Accept both."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(x) for x in json.loads(str(v))]


def box_from_props(props: dict[str, Any]) -> Box:
    """Rebuild a :class:`Box` from its AGE vertex properties (inverse of ``box_to_props``).

    Picks only the core Box fields, so it round-trips a registry box and also reads a
    domain-pack box (ignoring its pack-only extras — ``kind``/``content_hash``/
    ``entity_types``), giving one uniform box read across both. ``interest`` is
    reconstructed only when ``interest_role``/``interest_stake`` are present.
    """
    interest: SourceInterest | None = None
    if "interest_role" in props or "interest_stake" in props:
        role = props.get("interest_role")
        interest = SourceInterest(
            role=None if role is None else str(role),
            stake=frozenset(_as_str_list(props.get("interest_stake"))),
        )
    return Box(
        id=uuid.UUID(str(props["id"])),
        name=str(props["name"]),
        tier=Tier(str(props["tier"])),
        version=str(props["version"]),
        source=str(props["source"]),
        reliability_prior=float(props["reliability_prior"]),
        interest=interest,
        valid_from=datetime.fromisoformat(str(props["valid_from"])),
        valid_to=(
            None
            if props.get("valid_to") is None
            else datetime.fromisoformat(str(props["valid_to"]))
        ),
        status=BoxStatus(str(props["status"])),
    )


def resolve_tier(box: Box, override: Tier | None = None) -> Tier:
    """A node's effective tier: inherited from its ``Box`` unless explicitly overridden
    (§9/§10). Pure; consumed by the ``extract`` operator (G2.2) when it stamps Facts."""
    return override if override is not None else box.tier


def case_box(
    name: str,
    version: str,
    source: str,
    reliability_prior: float,
    *,
    interest: SourceInterest | None = None,
    valid_from: datetime | None = None,
) -> Box:
    """Construct a **case-evidence** Box (§9): a source box, append-on-ingest, holding a
    case document's observations/facts (the ``extract`` operator's write target, G2.2).

    The id is derived deterministically from ``(name, version)`` so re-ingesting the same
    case is idempotent (see ``_BOX_NAMESPACE``). ``valid_from`` defaults to now and is
    **create-only** — if the box already exists, the registry preserves the stored stamp
    and this freshly-built one is discarded (it never moves the bitemporal anchor).
    """
    return Box(
        id=box_id_for(name, version),
        name=name,
        tier=Tier.CASE,
        version=version,
        source=source,
        reliability_prior=reliability_prior,
        interest=interest,
        valid_from=valid_from or datetime.now(UTC),
        status=BoxStatus.ACTIVE,
    )


def box_id_for(name: str, version: str) -> uuid.UUID:
    """The deterministic box id for a ``(name, version)`` key (registry/case boxes)."""
    return uuid.uuid5(_BOX_NAMESPACE, f"{name}@{version}")
