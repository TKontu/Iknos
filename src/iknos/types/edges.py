"""Reasoning-graph edges (§10).

Two categories:
- Provenance edges (EVIDENCED_BY, DERIVED_FROM, INVOLVES) — no annotations.
- Evidential edges (SUPPORTS, REFUTES) — carry sign + strength + significance
  PLUS the two-annotation pair (§12).
"""

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from iknos.types.annotations import Annotations
from iknos.types.governance import Sensitivity
from iknos.types.temporal import BitemporalFields


class EdgeSign(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"


class SameAsState(StrEnum):
    """The ``state`` property on a ``SAME_AS`` identity edge (§5.2, §10).

    Identity is a defeasible, scored assertion, never a destructive id reassignment:
    the ``SAME_AS``-connected component *is* the canonical entity, and asserting/
    retracting an edge is a merge/split handled as belief revision. The state encodes
    the **conservative under-merge default** — over-merge fabricates contradictions and
    corrupts reasoning, so auto-merge happens only above a high confidence bar:

    - ``CONFIRMED`` — auto-merged; the edge joins its endpoints into one canonical
      component (the unit reasoning aggregates evidence over).
    - ``CANDIDATE`` — below the auto-merge bar: the endpoints stay **separate** but the
      link keeps the fragmentation visible and the evidence bridgeable, pending expert
      confirmation via soft override (§10.3). Candidates do **not** merge components.
    """

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"


class BindingState(StrEnum):
    """The ``state`` property on a ``REFERS_TO`` binding edge (§3.1, §10).

    Reference binding is a **separate, scored decision** — detecting that a mention needs
    a referent is robust, but choosing *which* entity is error-prone, so it is split out and
    kept defeasible (§3.1). The state mirrors :class:`SameAsState`'s conservative default:

    - ``CONFIRMED`` — a single referent cleared the high binding bar: the ``Mention``'s
      denotation is committed.
    - ``CANDIDATE`` — below that bar, or two referents tie: the binding stays **open**. The
      mention may carry *several* ``CANDIDATE`` ``REFERS_TO`` edges (the competing referents,
      §3.1 "may carry multiple candidate targets when ambiguous"), and a proposition resting
      on an un-confirmed mention is marked ``provisional`` and routed to expert triage.

    An ``unresolved`` mention (no referent above even the candidate bar) writes **no** edge —
    the absence of a ``REFERS_TO`` is itself the unresolved state, and still marks its
    proposition provisional.
    """

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"


class AnchorState(StrEnum):
    """The ``state`` property on an ``ANCHORS_TO`` entity-linking edge (§5.2, §9, §14, §10).

    Anchoring entity-links a case ``Actor``/``Object`` to a **domain-pack taxonomy** node —
    the *primary, reliable* identity/level path (§14: "anchor first"). The edge is directed
    (case entity → taxonomy node): the taxonomy node is the authoritative identity an anchored
    entity takes on (*anchor canonicalizes*, §5.2/§14). The state mirrors :class:`SameAsState`'s
    conservative default — an over-eager anchor mis-canonicalizes an entity and corrupts its
    level, so a near-miss stays open:

    - ``CONFIRMED`` — a single taxonomy node cleared the high bar (typically an exact
      normalized-label match within the active pack scope): the entity's anchor is committed,
      and the taxonomy node is its canonical identity / level source.
    - ``CANDIDATE`` — below that bar, or two taxonomy nodes tie (e.g. a cross-pack homonym —
      a "valve" in two active packs): the anchor stays **open**. The entity may carry several
      ``CANDIDATE`` ``ANCHORS_TO`` edges (the competing taxonomy nodes), pending expert
      disambiguation (§ phase risks: cross-domain ambiguity is resolved by pack scope + review).

    An entity with no taxonomy node above even the candidate bar writes **no** edge — the
    absence of an ``ANCHORS_TO`` is the un-anchored state (it falls back to induced levels, §14).
    """

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"


class MeronymyType(StrEnum):
    """The part-whole *type* tag on a ``directPartOf``/``partOf`` edge (§14, §10).

    Part-of is **not uniformly transitive** across meronymy types (Winston/Chaffin/Herrmann;
    Keet & Artale): only the **component-integral / functional-complex** subtype
    (gearbox ⊃ shaft ⊃ bearing ⊃ roller) is transitivity-safe, so abstraction roll-up — the
    ``partOf`` closure and ancestor views — runs **only** along it (:func:`is_transitive`).
    Member-collection, portion-mass, stuff-object, feature-activity and place-area are tagged
    and **excluded from blanket roll-up**, or wrong aggregations leak into coarse views (§14).
    """

    COMPONENT_INTEGRAL = "component-integral"  # the only transitivity-safe subtype
    MEMBER_COLLECTION = "member-collection"
    PORTION_MASS = "portion-mass"
    STUFF_OBJECT = "stuff-object"
    FEATURE_ACTIVITY = "feature-activity"
    PLACE_AREA = "place-area"


# The transitivity-safe subtypes (§14). A frozenset, not a per-member flag, so the
# transitivity rule has one definition the closure and any view code read.
_TRANSITIVE_MERONYMY: frozenset[MeronymyType] = frozenset({MeronymyType.COMPONENT_INTEGRAL})


def is_transitive(meronymy_type: MeronymyType) -> bool:
    """Whether ``partOf`` roll-up may run along this meronymy type (§14).

    Only ``COMPONENT_INTEGRAL`` is transitivity-safe; every other subtype is excluded from the
    transitive closure (and from blanket ancestor views) to keep wrong aggregations out of
    coarse-level presentation.
    """
    return meronymy_type in _TRANSITIVE_MERONYMY


class AttachmentProvenance(StrEnum):
    """How a part-whole edge's level attachment was produced (§14) — the ``provenance`` tag.

    Records *which acquisition path* set the level, so its confidence is interpretable:
    ``ANCHORED`` (entity-linked to a domain-pack taxonomy — the reliable, high-confidence
    path) vs ``INDUCED`` (text-induced meronymy — lower-confidence, human-review-gated) vs
    ``RELATIVE`` (last-resort relative ordering when no parent is named). The induce slice
    (G2.5) writes ``INDUCED``; anchoring is the deferred primary path.
    """

    ANCHORED = "anchored"
    INDUCED = "induced"
    RELATIVE = "relative"


class Role(StrEnum):
    """The ``role`` property on an ``INVOLVES`` edge (§10).

    ``INVOLVES`` links a reasoning node (Fact/Conclusion/Hypothesis) to an
    Actor/Object, tagged with the entity's role in the claim. The vocabulary is
    open in the same sense as ``TaskType`` — adding a member is additive (the
    value is a stored string; no migration) — so unusual roles do not force a
    break. The ``Involves`` Pydantic projection lands with Actor/Object in
    Phase 2; the property *contract* is fixed here now (the Phase 0 convention).

    **Why ``subject`` is privileged — abstraction level is *derived*, not stored
    (§14).** A reasoning node has no ``level`` property. Its abstraction level is
    the position of its **primary referent** — the entity on its ``subject``-role
    ``INVOLVES`` edge — in the ``partOf`` order (the part-whole DAG built by the
    domain layer, ``iknos.domain``; §14). Deriving level this way keeps it correct
    as the hierarchy is refined and relative by construction; a fact whose
    referent is ambiguous attaches at multiple levels rather than being forced to
    one. Forward (Phase 6 consumer, no Phase 0 code): an optional ``partOf`` depth/
    rank MAY be materialized for query performance, recomputed on any hierarchy
    change — a cache of the derived value, never an authoritative stored level.
    """

    SUBJECT = "subject"  # the primary referent — anchors derived abstraction level
    OBJECT = "object"
    INSTRUMENT = "instrument"


class EvidencedBy(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: uuid.UUID
    target: uuid.UUID


class EvidentialEdge(BaseModel):
    """SUPPORTS or REFUTES link between reasoning nodes.

    ``sensitivity`` is the lub of the edge's antecedents (§9.1; propagation walk
    deferred) and is distinct from ``faithfulness`` (§3.1) and from edge
    ``strength``/``significance`` (§8) — three separate quantities, never merged.
    """

    model_config = ConfigDict(frozen=True)

    source: uuid.UUID
    target: uuid.UUID
    box: uuid.UUID
    sign: EdgeSign
    strength: float = Field(..., ge=0.0, le=1.0)
    significance: float = Field(..., ge=0.0, le=1.0)
    annotations: Annotations
    temporal: BitemporalFields
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)
    override: dict[str, Any] | None = None
