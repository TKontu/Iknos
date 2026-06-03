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
from iknos.types.temporal import BitemporalFields


class EdgeSign(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"


class EvidencedBy(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: uuid.UUID
    target: uuid.UUID


class EvidentialEdge(BaseModel):
    """SUPPORTS or REFUTES link between reasoning nodes."""

    model_config = ConfigDict(frozen=True)

    source: uuid.UUID
    target: uuid.UUID
    box: uuid.UUID
    sign: EdgeSign
    strength: float = Field(..., ge=0.0, le=1.0)
    significance: float = Field(..., ge=0.0, le=1.0)
    annotations: Annotations
    temporal: BitemporalFields
    override: dict[str, Any] | None = None
