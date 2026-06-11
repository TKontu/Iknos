"""Governance value objects (§9.1). Schema contract now; logic deferred.

Two concerns, both carried on the graph from the start so later tracks have
somewhere to write:

- **Sensitivity** — a lattice (ordered level + compartment tags) that originates
  on Documents/Spans and propagates to derived reasoning nodes/edges as the
  least upper bound (``lub``) of its antecedents. The ``lub`` algebra is pure and
  lives here; the graph-walk that applies it over DERIVED_FROM/EVIDENCED_BY, and
  the clearance-relative access-control projection it drives, are deferred to the
  governance track.
- **SourceInterest** — the source's stake/role, an *input* to conditional
  credibility (§9.1). Credibility itself is derived, never stored (see ``Box``).
"""

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class SensitivityLevel(StrEnum):
    """Sensitivity lattice level (§9.1) — a label, ordered by ``_SENSITIVITY_RANK``.

    NOTE: this is a ``StrEnum``, so the inherited ``<`` compares *alphabetically*
    (``confidential`` < ``internal`` < …), which is NOT the lattice order. The
    enum's comparison operators are therefore undefined for governance — order is
    expressed only through ``_SENSITIVITY_RANK`` and ``Sensitivity.lub``. Keeping
    it a ``StrEnum`` preserves the plain-string serialization the AGE layer relies
    on (``db/age.py:cypher_map``).
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# Single source of truth for lattice order (low → high). Do not order via ``<``.
_SENSITIVITY_RANK: dict[SensitivityLevel, int] = {
    SensitivityLevel.PUBLIC: 0,
    SensitivityLevel.INTERNAL: 1,
    SensitivityLevel.CONFIDENTIAL: 2,
    SensitivityLevel.RESTRICTED: 3,
}


class Sensitivity(BaseModel):
    """A lattice point: a level plus compartment tags (§9.1).

    On a derived node/edge this is the least upper bound of its antecedents'
    sensitivity — the information-flow high-water-mark. ``lub`` is that join;
    applying it across the provenance graph is deferred to the governance track.
    """

    model_config = ConfigDict(frozen=True)

    level: SensitivityLevel = SensitivityLevel.PUBLIC
    compartments: frozenset[str] = frozenset()

    def lub(self, other: "Sensitivity") -> "Sensitivity":
        """Least upper bound: the higher level and the union of compartments.

        This is the documented max-propagation rule (§9.1): a derived node is
        never less sensitive than any antecedent, and inherits every compartment.
        """
        higher = max(self.level, other.level, key=lambda level: _SENSITIVITY_RANK[level])
        return Sensitivity(level=higher, compartments=self.compartments | other.compartments)

    def flatten(self) -> dict[str, Any]:
        """Canonical flat graph properties for ``cypher_map`` (queryable in Cypher).

        Returns ``sensitivity_level`` (str) and ``sensitivity_compartments``
        (sorted list). Use this when persisting — a nested ``model_dump`` would be
        JSON-blobbed into one opaque property (and ``frozenset`` is not even
        JSON-serializable), defeating access-control filtering. The flat names are
        the convention later persistence/read code must match.
        """
        return {
            "sensitivity_level": str(self.level),
            "sensitivity_compartments": sorted(self.compartments),
        }

    @classmethod
    def from_props(cls, props: dict[str, Any]) -> "Sensitivity":
        """Read a :class:`Sensitivity` back from flat AGE properties — inverse of
        :meth:`flatten` (cf. ``boxes/serde.box_from_props``).

        ``cypher_map`` writes ``sensitivity_compartments`` as a JSON-encoded **string**
        (a list is not a native agtype scalar), so it comes back as JSON text to decode;
        a real list (defensive) or an absent/null value is tolerated. An absent
        ``sensitivity_level`` defaults to the lattice origin (``PUBLIC``) — the §9.1 floor
        for an un-annotated node, so the propagation seed is never below it.
        """
        level_raw = props.get("sensitivity_level")
        level = SensitivityLevel(level_raw) if level_raw else SensitivityLevel.PUBLIC

        comp_raw = props.get("sensitivity_compartments")
        if comp_raw is None or comp_raw == "":
            compartments: frozenset[str] = frozenset()
        elif isinstance(comp_raw, str):
            compartments = frozenset(json.loads(comp_raw))
        else:
            compartments = frozenset(comp_raw)
        return cls(level=level, compartments=compartments)


class SourceInterest(BaseModel):
    """A source's stake/role — an input to conditional credibility (§9.1).

    Structured (not a bare string) so it can grow without a breaking field-type
    change. The typical role/stake patterns are domain knowledge populated by the
    domain pack (G0.7); per-claim alignment is LLM/expert-flagged at extraction
    (Phase 1+). ``None`` (unknown) is distinct from ``SourceInterest()`` (a known,
    empty stake).
    """

    model_config = ConfigDict(frozen=True)

    role: str | None = None
    stake: frozenset[str] = frozenset()

    def flatten(self) -> dict[str, Any]:
        """Canonical flat graph properties for ``cypher_map`` (queryable in Cypher).

        Returns ``interest_role`` (str | None) and ``interest_stake`` (sorted list),
        mirroring :meth:`Sensitivity.flatten` — flat, named properties rather than a
        nested ``model_dump`` blob, so the conditional-credibility track (§9.1) can
        read a stable contract. A caller that holds ``None`` (interest unknown) must
        omit these keys entirely; emitting them implies a *known* (possibly empty)
        stake — the ``None`` vs ``SourceInterest()`` distinction this class preserves.
        """
        return {
            "interest_role": self.role,
            "interest_stake": sorted(self.stake),
        }


class InterestAlignment(StrEnum):
    """The per-claim alignment between a claim and its source's interest (§9.1).

    The **derived per-claim input** to conditional credibility (§10 ``credibility``): given
    a source's ``SourceInterest`` (role/stake, from the domain pack) and a specific claim,
    whether the claim *serves* the source's interest (discount — a bearing supplier blaming
    transport for its own component's failure), is *neutral*, or runs *against* it (boost — an
    admission against interest is a recognized reliability signal). A ``StrEnum`` so it
    serializes to a plain AGE property string like the epistemic enums.

    Per-claim alignment is **LLM/expert-flagged, defeasible, overridable, and logged** (§9.1)
    — the judging pass is a later increment, so a Fact carries ``None`` until then (the
    schema-contract-placeholder convention, cf. ``Proposition.faithfulness``).
    :func:`iknos.core.credibility.effective_credibility` treats an absent alignment as
    ``UNKNOWN`` (the identity modifier — defer, never penalize on absence).
    """

    SELF_SERVING = "self-serving"
    NEUTRAL = "neutral"
    AGAINST_INTEREST = "against-interest"
    UNKNOWN = "unknown"
