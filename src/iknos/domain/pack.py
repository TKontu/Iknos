"""Domain-pack declaration + part-whole closure (G0.7; architecture.md §9, §14).

The epistemic schema (Facts, Actors, Objects, conclusions, the evidential/
provenance edges of §10) is **fixed and domain-agnostic**. The *domain layer* —
the entity *types*, the part-whole taxonomy (§14), and (later) the domain rules —
is **pluggable**. A **domain pack** packages that layer as one reference/schema-
tier ``Box`` bundling:

- an **entity-type ontology** (the legal ``type`` values for the taxonomy's
  Objects, with an optional subtype-of hierarchy), and
- a **part-whole taxonomy**: Objects connected by ``directPartOf`` edges, each
  tagged with a meronymy type; ``partOf`` is the transitive closure (§10, §14).

Pure module: declaration is validated here with no database access, so a
malformed pack fails fast and can never half-load (the loader, `loader.py`, only
runs against an already-validated pack). The closure algebra also lives here so
it is unit-testable without a live graph.

**Deferred, with seams (architecture.md §9):** domain rules (the clingo
deductive/defeasible rules of §8) and a reference-hypothesis set (FMEA /
differential-diagnosis libraries that seed candidate answers for a Task, §11.2)
are part of a full pack but are not modelled in Phase 0 — they attach to the same
``Box`` in the phase that consumes them. Investigation-scoped *activation* (an
investigation activates the packs it needs, §9) likewise lands with the Task/
investigation entity (Phase 6); until then a loaded pack's ``Box.status`` is the
activation flag (see ``loader.list_active_packs``).
"""

import hashlib
import json
import uuid
from collections import deque
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from iknos.types.nodes import Box, BoxStatus, Tier

# Stable namespace for pack-derived UUIDv5 ids. Deriving ids from (pack, version,
# key) instead of random uuid4 makes a load **idempotent and reproducible**: the
# same pack always maps to the same Box/Object ids, so re-activation is a no-op
# (the loader MERGEs on id) and Phase 1 entity-linking can address a taxonomy node
# by recomputing its id. Never change this value — it would orphan every loaded
# pack. (Constant, project-private DNS-style namespace.)
_PACK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "domain-pack.iknos")


class MeronymyType(StrEnum):
    """Part-whole (meronymy) subtype (§14; Winston/Chaffin/Herrmann taxonomy).

    Transitivity is **not** uniform across these: only ``component-integral`` is
    transitivity-safe, so ``partOf`` roll-up follows component-integral chains
    only (see ``_ROLLUP_SAFE`` and ``DomainPack.transitive_closure``). The others
    are representable as single-hop ``directPartOf``/``partOf`` relations but do
    not compose across hops (§10, §14).
    """

    COMPONENT_INTEGRAL = "component-integral"  # roller–bearing; the rollup-safe one
    MEMBER_COLLECTION = "member-collection"  # tree–forest
    PORTION_MASS = "portion-mass"  # slice–pie
    STUFF_OBJECT = "stuff-object"  # steel–bearing
    FEATURE_ACTIVITY = "feature-activity"  # paying–shopping
    PLACE_AREA = "place-area"  # oasis–desert


# The transitivity-safe subtypes — the only ones a multi-hop ``partOf`` rolls up
# through (§14). Single source of truth; widen only with an explicit §14 argument.
_ROLLUP_SAFE: frozenset[MeronymyType] = frozenset({MeronymyType.COMPONENT_INTEGRAL})


class EntityType(BaseModel):
    """One entry in a pack's entity-type ontology (architecture.md §9, §10).

    Defines a legal ``type`` value for the pack's taxonomy Objects. ``parent``
    expresses a subtype-of hierarchy within the ontology (e.g. ``Bearing`` is-a
    ``Component``); it is metadata for now. Forward path: if entity types need
    their own edges (cross-pack subtype reuse, type-level rules) they graduate
    from Box metadata to first-class graph nodes — the ``name``/``parent`` shape
    here is chosen so that promotion is additive.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1)
    description: str | None = None
    parent: str | None = None  # subtype-of, references another EntityType.name


class TaxonomyEntity(BaseModel):
    """A node in the pack's part-whole taxonomy — persisted as an ``Object`` (§10).

    ``key`` is a stable, human-readable identifier **within the pack** used to
    reference the entity from ``part_of`` relations; the persisted graph id is
    derived deterministically from ``(pack, version, key)`` (see
    ``DomainPack.entity_id``), so keys, not raw uuids, are what a pack author
    writes and what survives a re-version.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)  # must be a declared EntityType.name


class PartOfRelation(BaseModel):
    """A single **direct** part-whole step — persisted as a ``directPartOf`` edge.

    ``part`` and ``whole`` are ``TaxonomyEntity.key`` references. The part-whole
    structure is a DAG (an entity may have several wholes, §10/§14); cycles are
    rejected at validation time.
    """

    model_config = ConfigDict(frozen=True)

    part: str = Field(..., min_length=1)
    whole: str = Field(..., min_length=1)
    meronymy: MeronymyType = MeronymyType.COMPONENT_INTEGRAL


class PartOfEdge(BaseModel):
    """A materialized ``partOf`` edge — an element of the transitive closure.

    ``derivation`` distinguishes the base relation (``direct`` — a declared
    ``directPartOf``) from a rolled-up ancestor reached over a component-integral
    chain (``rollup``). ``partOf`` is **derived** from ``directPartOf`` (§14): it
    is materialized at load for query performance but must be recomputed whenever
    the taxonomy changes.
    """

    model_config = ConfigDict(frozen=True)

    part: str
    whole: str
    meronymy: MeronymyType
    derivation: str  # "direct" | "rollup"


class DomainPack(BaseModel):
    """A versioned, declarable domain layer (architecture.md §9).

    A pack is **declared** as data (see ``from_file``), **versioned** by ``name``
    + ``version`` (which together determine its deterministic Box id, so a new
    version is a new Box rather than a destructive in-place edit — old versions
    deprecate, never vanish, preserving audit/belief-revision), and **activated**
    per investigation (Phase 6 seam; ``loader`` treats an active Box as activated
    for now).

    All structural invariants are enforced here at construction — the loader
    assumes a valid pack and never partially writes a bad one.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    tier: Tier = Tier.REFERENCE
    source: str = Field(..., min_length=1)
    reliability_prior: float = Field(..., ge=0.0, le=1.0)
    description: str | None = None
    entity_types: list[EntityType] = Field(default_factory=list)
    entities: list[TaxonomyEntity] = Field(default_factory=list)
    part_of: list[PartOfRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_structure(self) -> "DomainPack":
        # A pack is a reference/schema-tier artifact (§9): durable, shared,
        # read-only domain knowledge. Case/working are per-investigation tiers.
        if self.tier not in (Tier.SCHEMA, Tier.REFERENCE):
            raise ValueError(f"domain pack tier must be 'schema' or 'reference', got '{self.tier}'")

        type_names = [t.name for t in self.entity_types]
        if len(set(type_names)) != len(type_names):
            raise ValueError("duplicate entity-type names in ontology")
        type_set = set(type_names)
        for t in self.entity_types:
            if t.parent is not None and t.parent not in type_set:
                raise ValueError(f"entity-type '{t.name}' parent '{t.parent}' is not declared")

        keys = [e.key for e in self.entities]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate taxonomy entity keys")
        key_set = set(keys)
        for e in self.entities:
            if e.type not in type_set:
                raise ValueError(
                    f"entity '{e.key}' has type '{e.type}' not in the entity-type ontology"
                )

        for rel in self.part_of:
            if rel.part not in key_set:
                raise ValueError(f"part_of references unknown part key '{rel.part}'")
            if rel.whole not in key_set:
                raise ValueError(f"part_of references unknown whole key '{rel.whole}'")
            if rel.part == rel.whole:
                raise ValueError(f"part_of self-loop on '{rel.part}'")
        if len({(r.part, r.whole) for r in self.part_of}) != len(self.part_of):
            raise ValueError("duplicate part_of relation")

        self._reject_cycles(key_set)
        return self

    def _reject_cycles(self, key_set: set[str]) -> None:
        """Part-whole must be a DAG (§10/§14). Kahn's algorithm; reports a witness."""
        adj: dict[str, list[str]] = {k: [] for k in key_set}
        indeg: dict[str, int] = {k: 0 for k in key_set}
        for rel in self.part_of:
            adj[rel.part].append(rel.whole)
            indeg[rel.whole] += 1
        queue = deque(k for k, d in indeg.items() if d == 0)
        visited = 0
        while queue:
            n = queue.popleft()
            visited += 1
            for w in adj[n]:
                indeg[w] -= 1
                if indeg[w] == 0:
                    queue.append(w)
        if visited != len(key_set):
            cyclic = sorted(k for k, d in indeg.items() if d > 0)
            raise ValueError(f"part-whole hierarchy is cyclic (involves {cyclic})")

    # --- deterministic ids (see _PACK_NAMESPACE) ---

    @property
    def box_id(self) -> uuid.UUID:
        """Stable Box id for this (name, version). Reproducible across loads."""
        return uuid.uuid5(_PACK_NAMESPACE, f"{self.name}@{self.version}")

    def entity_id(self, key: str) -> uuid.UUID:
        """Stable Object id for a taxonomy key, namespaced under this pack's Box."""
        return uuid.uuid5(self.box_id, key)

    @property
    def content_hash(self) -> str:
        """A stable SHA-256 over the pack's *semantic* declaration (G0.R1).

        Persisted on the Box at first load so the loader can enforce pack
        **immutability**: a re-load whose content matches is a true no-op (the
        bitemporal ``valid_from`` is never rewritten), while a re-load whose
        content differs under the **same** ``(name, version)`` is rejected rather
        than silently diverging from the declaration (see ``loader.load_pack``).

        Covers *content only*, not identity: ``name``/``version`` are excluded
        because they are already the Box identity (``box_id``), and a hash is only
        ever compared between two packs that share a ``box_id``. So the hash
        answers "did the content change?", while ``box_id`` answers "is this the
        same pack version?".

        Canonicalized so the hash tracks *meaning*, not formatting: collections
        are sorted (``entity_types`` by name, ``entities`` by key, ``part_of`` by
        ``(part, whole)``) and meronymy is normalized to its string value, so
        reordering or reindenting the JSON does **not** trip the immutability
        guard — only a genuine change to a persisted field does.
        """
        payload = {
            "tier": str(self.tier),
            "source": self.source,
            "reliability_prior": self.reliability_prior,
            "description": self.description,
            "entity_types": sorted(
                (
                    {"name": t.name, "description": t.description, "parent": t.parent}
                    for t in self.entity_types
                ),
                key=lambda t: str(t["name"]),
            ),
            "entities": sorted(
                ({"key": e.key, "label": e.label, "type": e.type} for e in self.entities),
                key=lambda e: e["key"],
            ),
            "part_of": sorted(
                (
                    {"part": r.part, "whole": r.whole, "meronymy": str(r.meronymy)}
                    for r in self.part_of
                ),
                key=lambda r: (r["part"], r["whole"]),
            ),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # --- closure (§14) ---

    def transitive_closure(self) -> list[PartOfEdge]:
        """The materialized ``partOf`` edge set: closure of ``directPartOf`` (§14).

        - every declared relation is a 1-hop ``partOf`` (``derivation='direct'``),
          carrying its own meronymy type — any subtype is a valid single step;
        - additionally, for every node, ancestors reachable over a chain whose
          edges are **all** component-integral (length ≥ 2) get a rolled-up
          ``partOf`` (``derivation='rollup'``, ``meronymy=component-integral``).
          Roll-up stops at the first non-rollup-safe edge — mixed chains do not
          compose (transitivity is unsafe outside component-integral, §14).

        Deterministic order (direct relations in declaration order, then rollups
        sorted) so loads and tests are reproducible.
        """
        edges: list[PartOfEdge] = [
            PartOfEdge(part=r.part, whole=r.whole, meronymy=r.meronymy, derivation="direct")
            for r in self.part_of
        ]

        # Adjacency over component-integral edges only — the rollup-safe subgraph.
        safe_adj: dict[str, list[str]] = {e.key: [] for e in self.entities}
        for r in self.part_of:
            if r.meronymy in _ROLLUP_SAFE:
                safe_adj[r.part].append(r.whole)

        seen_direct = {(r.part, r.whole) for r in self.part_of}
        rollups: set[tuple[str, str]] = set()
        for start in safe_adj:
            # BFS over component-integral edges; record ancestors ≥ 2 hops away
            # that aren't already a direct relation.
            queue = deque((nxt, 1) for nxt in safe_adj[start])
            local_seen = set(safe_adj[start])
            while queue:
                node, depth = queue.popleft()
                if depth >= 2 and (start, node) not in seen_direct:
                    rollups.add((start, node))
                for nxt in safe_adj[node]:
                    if nxt not in local_seen:
                        local_seen.add(nxt)
                        queue.append((nxt, depth + 1))

        edges.extend(
            PartOfEdge(
                part=p,
                whole=w,
                meronymy=MeronymyType.COMPONENT_INTEGRAL,
                derivation="rollup",
            )
            for p, w in sorted(rollups)
        )
        return edges

    # --- projections ---

    def to_box(self, valid_from: datetime) -> Box:
        """The registry ``Box`` for this pack (§9), validated by the core model.

        Pack-specific graph properties (the entity-type ontology, the pack marker)
        are added by the loader as extra AGE properties; they are intentionally
        not on the core ``Box`` Pydantic model, which stays domain-agnostic.
        """
        return Box(
            id=self.box_id,
            name=self.name,
            tier=self.tier,
            version=self.version,
            source=self.source,
            reliability_prior=self.reliability_prior,
            valid_from=valid_from,
            status=BoxStatus.ACTIVE,
        )

    # --- declaration loading ---

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainPack":
        return cls.model_validate(data)

    @classmethod
    def from_file(cls, path: str | Path) -> "DomainPack":
        """Load and validate a pack declaration from a JSON file."""
        text = Path(path).read_text(encoding="utf-8")
        return cls.model_validate(json.loads(text))
