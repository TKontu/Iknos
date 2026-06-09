"""Domain layer (architecture.md §9).

The epistemic schema is fixed and domain-agnostic (``iknos.types``); the domain
layer — entity types, the part-whole taxonomy (§14), and later the domain rules —
is pluggable, packaged as **domain packs** (reference/schema-tier boxes).

Layering: the **declaration** layer (``iknos.domain.pack``) is pure and has no
database dependency, so it imports anywhere (incl. unit tests with no
``DATABASE_URL``). The **persistence** layer is imported explicitly from
``iknos.domain.loader`` — kept out of this package ``__init__`` so importing a
pack declaration never drags in the DB/config stack.
"""

from iknos.domain.pack import (
    DomainPack,
    EntityType,
    MeronymyType,
    PartOfEdge,
    PartOfRelation,
    TaxonomyEntity,
)

__all__ = [
    "DomainPack",
    "EntityType",
    "MeronymyType",
    "PartOfEdge",
    "PartOfRelation",
    "TaxonomyEntity",
]
