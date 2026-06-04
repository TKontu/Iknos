"""Reasoning-graph nodes (§10). Pydantic projections of AGE vertices.

Phase 0 covers the minimal set needed for the exit-criteria smoke test.
Remaining labels (Actor, Object, DeductiveConclusion, InductiveConclusion,
Hypothesis) are pre-created in the AGE graph by the initial migration but get
their Pydantic models in later phases. Proposition lands in Phase 1 Increment 3.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from iknos.types.annotations import Annotations
from iknos.types.temporal import BitemporalFields


class Tier(StrEnum):
    """Box reasoning tier (§9)."""

    AXIOM = "axiom"
    DOMAIN = "domain"
    EVIDENCE = "evidence"
    DERIVED = "derived"


class BoxStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    title: str | None = None


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    document_id: uuid.UUID
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)


class Proposition(BaseModel):
    """A decontextualized atomic statement (§3, §10).

    Provenance is carried by the EVIDENCED_BY edge to the source Span(s), never
    embedded here — so no document_id field (it is reachable via the span). `box`
    is deferred to Phase 2, which owns boxing/tiers (tracked deviation from §10).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    text: str


class Box(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    name: str
    tier: Tier
    version: str
    source: str
    reliability_prior: float = Field(..., ge=0.0, le=1.0)
    valid_from: datetime
    valid_to: datetime | None = None
    status: BoxStatus = BoxStatus.ACTIVE


class Fact(BaseModel):
    """Phase 0 minimal reasoning node."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    box: uuid.UUID
    tier: Tier
    statement: str
    annotations: Annotations
    temporal: BitemporalFields
    override: dict[str, Any] | None = None  # §10.3 — logic lands in Phase 7
