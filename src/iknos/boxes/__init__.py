"""Box subsystem (§9) — the lifecycle/provenance unit every node and edge carries.

``serde`` owns the pure Box↔AGE property contract and constructors (DB-free, so it is
unit-testable and importing it never loads the config singleton); ``registry`` is the
DB-backed management surface (create / read / scope / deprecate). The domain-pack loader
writes pack boxes through the same ``serde`` contract so the two paths cannot diverge.
"""

from iknos.boxes.registry import (
    REGISTRY_ACTOR,
    active_boxes_by_tier,
    create_box,
    deprecate_box,
    get_box,
    list_boxes,
)
from iknos.boxes.serde import (
    box_from_props,
    box_id_for,
    box_to_props,
    case_box,
    resolve_tier,
)

__all__ = [
    "REGISTRY_ACTOR",
    "active_boxes_by_tier",
    "box_from_props",
    "box_id_for",
    "box_to_props",
    "case_box",
    "create_box",
    "deprecate_box",
    "get_box",
    "list_boxes",
    "resolve_tier",
]
