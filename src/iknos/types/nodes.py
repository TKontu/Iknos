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
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
)
from iknos.types.governance import Sensitivity, SourceInterest
from iknos.types.temporal import BitemporalFields


class Tier(StrEnum):
    """Box reasoning tier (§9; architecture.md §10).

    Revised-plan vocabulary. Mapping from the old plan for anyone holding a dev
    graph (AGE stores ``tier`` as a plain property string, so no data migration):
    ``axiom→schema``, ``domain→reference``, ``evidence→case``, ``derived→working``.
    """

    SCHEMA = "schema"
    REFERENCE = "reference"
    CASE = "case"
    WORKING = "working"


class BoxStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    title: str | None = None
    # Lattice origin (§9.1): sensitivity is seeded here and propagates upward.
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)


class Span(BaseModel):
    """A contiguous source range — the unit of provenance (§10).

    ``start``/``end`` are character offsets into the document's text; ``level`` is
    the segmentation level that produced the span (single-level=0 for now; G1.10
    adds coarser levels). ``layout`` is the optional visual-provenance handle from
    the parse front-end (§1, G1.0): the ``{page, bbox}`` region(s) on the original
    page image so a claim resolves to a place on the page, not just a char offset.
    It is ``None`` when ingesting plain text (no parser); the parser owns its
    internal shape, so it is stored opaquely here.
    """

    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    document_id: uuid.UUID
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    level: int = Field(default=0, ge=0)
    layout: dict[str, Any] | None = None
    # Lattice origin (§9.1); may differ from its Document (e.g. a redacted span).
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)


class Proposition(BaseModel):
    """A decontextualized atomic statement (§3, §10).

    Provenance is carried by the EVIDENCED_BY edge to the source Span(s), never
    embedded here — so no document_id field (it is reachable via the span). `box`
    is deferred to Phase 2, which owns boxing/tiers (tracked deviation from §10).

    Structured epistemic fields (§3.1, G1.1) are kept distinct, never flattened into
    ``text`` (see `types/epistemic.py`). Like ``Tier``, they are AGE property strings,
    so adding them needs no data migration. Two are **schema-contract placeholders**
    here: ``faithfulness`` (calibrated — owned by the multi-sample/verify increments
    G1.4/G1.5) and ``provisional`` (the system gate, G1.6) are ``None`` until those
    land — never a self-reported value (§3.1: confidence is not verbalized self-report).
    ``routing`` is a cached derivation of ``epistemic_class`` (invariant
    ``routing == route_for(epistemic_class)``), never set independently.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    text: str
    polarity: Polarity = Polarity.ASSERTED
    modality: Modality = Modality.CATEGORICAL
    attribution: Attribution = Attribution.DOCUMENT
    scope: str = ""
    epistemic_class: EpistemicClass = EpistemicClass.OBSERVATION
    routing: Routing = Routing.FACT
    faithfulness: float | None = None
    provisional: bool | None = None


class Box(BaseModel):
    """Lifecycle/provenance unit and source descriptor (§9).

    Effective credibility is **derived, not stored** (cf. abstraction level, §14):
    ``reliability_prior × f(interest_alignment, epistemic_class)``, computed at
    use-time, gated by epistemic class (minor for observation/measurement, central
    for judgement) and belief-revised by track record (§9.1). The *inputs* live
    here (``reliability_prior``, ``interest``); ``epistemic_class`` arrives on
    ``Proposition`` in Phase 1 (G1.1); the per-claim ``interest_alignment`` is a
    derived annotation written at extraction (Phase 1+/LLM-expert). A flat stored
    credibility scalar is deliberately avoided — it would collapse the conditional
    nature the spec forbids.

    Forward, not Phase 0: a clearly-derived ``credibility_cached`` recomputed on
    input change if perf demands; modeling a source as an ``Actor`` carrying
    ``reliability_prior`` + ``SourceInterest`` + ``track_record`` for
    cross-investigation revision; a per-``Document`` interest override when a box
    genuinely spans sources.
    """

    model_config = ConfigDict(frozen=True)
    id: uuid.UUID
    name: str
    tier: Tier
    version: str
    source: str
    reliability_prior: float = Field(..., ge=0.0, le=1.0)
    # Source stake/role — input to conditional credibility (§9.1). None = unknown.
    interest: SourceInterest | None = None
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
    # lub of antecedents' sensitivity (§9.1); propagation walk deferred.
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)
    override: dict[str, Any] | None = None  # §10.3 — logic lands in Phase 7
