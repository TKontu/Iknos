"""Reference-corpus read-only seal (Phase 1, G1.8) — the §6.1 amortization marker.

§6.1 splits ingest into **two regimes**: a large but *static* reference corpus (industry
knowledge, domain packs) is embedded/segmented/extracted **once** and reused read-only
across every investigation, while only the investigation's own case documents are
processed per investigation. Content-addressed caching (G1.7/G1.7b) already makes a
*re-run* of identical content a no-op, but it still pays the embedding pass before the
write-time guard short-circuits — so "amortized, not repaid each time" was not yet real.

This module is the regime marker that closes that gap. Ingesting a document into a
**reference/schema-tier** box records a ``(:Document)-[:MEMBER_OF]->(:Box)`` **seal**:
the document's content digest, its tier, and a ``sealed`` flag. The seal is what lets a
later investigation recognise the corpus is already present and **skip the whole pipeline**
(no embed, no segment, no LLM) instead of repaying it — see
``core/ingest.py::ingest_reference_document``. It also makes the corpus read-only: a
changed-content re-ingest under the same document id fails loud (``ReferenceSealError``,
mirroring ``domain.loader.PackImmutabilityError``) rather than silently re-processing
entrenched reference knowledge — bump the version / use a new id, exactly as a domain
pack does.

(Distinct from ``core/reference.py``, the Phase 2 reference-*binding* subsystem — that
resolves a proposition's mentions to entities; this seals a reference *corpus* read-only.)

Two disciplines, inherited from the box layer:

- **Read-only by tier.** Only ``reference``/``schema`` boxes are sealable (``case``/
  ``working`` are the per-investigation, mutable regimes). The pure
  :func:`validate_sealable_tier` guard refuses a case box up front, so a caller cannot
  amortize evidence that is meant to be re-judged each investigation.
- **Caller owns the transaction.** Like the box registry and the pack loader, the write
  function issues its statements on the passed session and does **not** commit; the
  caller's single commit makes the spans and the seal appear atomically (a committed seal
  implies committed spans — the G0.R1 atomicity argument).

``iknos.db.age`` is imported lazily inside the DB-touching functions so importing this
module for the pure :func:`validate_sealable_tier` / :func:`document_input_sha256`
helpers does not pull in the ``DATABASE_URL`` config singleton — the same discipline as
``core/ingest.py`` and ``boxes/registry.py``.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.provenance.action_log import record_action
from iknos.types.nodes import Box, Tier

# Action actor for reference sealing — distinct from "segmenter"/"parser"/"box-registry",
# so the §6.1 reference regime is its own auditable lifecycle in the actions log.
REFERENCE_ACTOR = "reference-ingest"

# The document↔box membership edge. A reference document is a MEMBER_OF its corpus box;
# the edge carries the read-only seal (tier + digest + sealed flag).
_MEMBER_OF = "MEMBER_OF"

# The read-only tiers (§9 order: schema → reference → case → working). Only these may be
# sealed: case/working are the per-investigation, mutable regimes that §6.1 keeps *out*
# of amortization. schema is included for symmetry (a schema-tier domain pack is equally
# static), though the common target is the reference tier.
_SEALABLE_TIERS = (Tier.SCHEMA, Tier.REFERENCE)


class ReferenceSealError(Exception):
    """A sealed reference document was re-ingested with **changed content** (or into a
    **different box**) under the same document id (G1.8).

    A reference corpus is immutable per ``(document id, content)``, exactly as a domain
    pack is immutable per ``(name, version)``: silently re-processing would repay the
    amortized cost §6.1 exists to avoid *and* let entrenched reference knowledge drift
    from what dependent conclusions were derived against. Use a new document id (or bump
    the corpus version) instead — mirrors ``domain.loader.PackImmutabilityError``.
    """


@dataclass(frozen=True)
class ReferenceSeal:
    """The read-only seal read back off a ``(:Document)-[:MEMBER_OF]->(:Box)`` edge."""

    box_id: uuid.UUID
    tier: Tier
    input_sha256: str  # digest of the document's raw bytes/text — the immutability key
    valid_from: datetime


def validate_sealable_tier(tier: Tier) -> None:
    """Refuse a seal on a non-reference tier (pure; the up-front G1.8 guard).

    Only ``reference``/``schema`` boxes hold the static, amortized knowledge §6.1 reuses
    read-only. A ``case``/``working`` box is the per-investigation regime — its documents
    go through :func:`~iknos.core.ingest.ingest_document` and are processed each
    investigation, never sealed.
    """
    if tier not in _SEALABLE_TIERS:
        raise ValueError(
            f"a read-only reference seal requires a reference/schema-tier box, got tier "
            f"{tier!r}; case documents use ingest_document (processed per investigation, §6.1)"
        )


def document_input_sha256(raw_text: str) -> str:
    """The content digest that keys a reference document's immutability (pure).

    Keyed on the document's own bytes — *not* the parse/segment content hash, which folds
    in parser/segmenter identity — so the seal's immutability key is the content itself.
    The bytes-in path digests ``document_bytes`` directly (same key the parse cache uses).
    """
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


async def get_reference_seal(session: AsyncSession, document_id: uuid.UUID) -> ReferenceSeal | None:
    """The read-only seal on this document, or ``None`` if it was never sealed.

    One round-trip over the ``MEMBER_OF`` edge: returns the box it is sealed into, the
    sealed tier, the content digest (for the immutability check), and the seal's
    ``valid_from``. A document is sealed into at most one reference box (the
    one-box-per-corpus-document invariant the ingest path enforces), so the first match
    is authoritative.
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    rows = await execute_cypher(
        session,
        f"MATCH (d:Document {{id: '{document_id}'}})-[r:{_MEMBER_OF}]->(b:Box) "
        "RETURN b.id, r.tier, r.input_sha256, r.valid_from",
        returns="box_id agtype, tier agtype, input_sha256 agtype, valid_from agtype",
    )
    if not rows:
        return None
    box_id, tier, input_sha256, valid_from = rows[0]
    return ReferenceSeal(
        box_id=uuid.UUID(unquote_agtype(box_id)),
        tier=Tier(unquote_agtype(tier)),
        input_sha256=unquote_agtype(input_sha256),
        valid_from=datetime.fromisoformat(unquote_agtype(valid_from)),
    )


async def seal_reference_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    box: Box,
    *,
    input_sha256: str,
    valid_from: datetime | None = None,
) -> None:
    """Seal a document read-only into its reference ``box`` (caller commits).

    MERGEs the ``(:Document)-[:MEMBER_OF]->(:Box)`` edge — both endpoints must already
    exist (the Document vertex is written by the ingest pipeline; the Box by the
    registry/pack loader) — and records a ``seal-reference`` Action. ``valid_from`` is
    **create-only** in practice: the ingest path only seals on first ingest (a re-ingest
    either reuses or raises before reaching here), so the seal's bitemporal anchor is
    never moved. Refuses a non-reference tier via :func:`validate_sealable_tier`.
    """
    from iknos.db.age import merge_edge

    validate_sealable_tier(box.tier)
    when = (valid_from or datetime.now(UTC)).isoformat()
    await merge_edge(
        session,
        src_id=document_id,
        dst_id=box.id,
        label=_MEMBER_OF,
        props={
            "tier": str(box.tier),
            "sealed": True,
            "input_sha256": input_sha256,
            "valid_from": when,
        },
    )
    await record_action(
        session,
        actor=REFERENCE_ACTOR,
        action_type="seal-reference",
        inputs={
            "document_id": str(document_id),
            "box": str(box.id),
            "tier": str(box.tier),
            "input_sha256": input_sha256,
        },
        outputs={"document_id": str(document_id), "box": str(box.id)},
    )
