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
