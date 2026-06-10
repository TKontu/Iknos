"""Span persistence + ingest orchestration (Phase 1, G1.9).

Segmentation (``core/segmentation.py``) yields in-memory ``(start_char, end_char)``
tuples; this module is what writes them down — ``Span`` vertices in the AGE graph
plus their dense embeddings in ``document_embeddings`` — so the proposition layer
(``core/proposition.py``) and downstream retrieval have real persisted spans to
hang off (today the proposition test hand-creates them). This closes the
end-to-end ingest blocker (``docs/gap_phase_1_ingest.md`` G1.9).

Robustness model (mirrors the domain-pack loader, ``domain/loader.py``):

- **Deterministic ids.** A span's id is ``uuid5(document_id, "start:end:level")`` —
  reproducible, so MERGE-on-id and the dense upsert are naturally idempotent and a
  re-run never duplicates. ``level`` is in the id so multi-level spans (G1.10) are
  additive.
- **Immutable per (document, segmentation).** A document's segmentation is keyed by
  a ``content_hash`` of its *inputs* (raw text + segmenter params + model). An
  identical re-run is a true no-op; re-segmenting with **changed** inputs raises
  :class:`DocumentResegmentationError` rather than silently orphaning the old spans
  and the propositions hanging off them (cascade re-ingest is deferred — G1.7).
- **Caller-owned transaction.** Neither ``persist_spans`` nor ``ingest_document``
  commits; the caller wraps the whole ingest in one transaction and commits on
  success. So a committed segmentation Action implies committed spans (the G0.R1
  atomicity argument), and a raised guard rolls back any ``document_content`` update.

``iknos.db.age`` is imported lazily inside the DB-touching functions so importing
this module (for the pure helpers) does not pull in the ``DATABASE_URL`` config
singleton — unit tests of ``split_sentences`` / ``span_content_hash`` / ``span_id_for``
stay DB-free, exactly as ``core/proposition.py`` does for its inference path.
"""

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.embeddings import EmbeddingSubstrate
from iknos.core.parse import NullParser, layouts_for_spans, parse_content_hash
from iknos.core.segmentation import SegmentationBackbone
from iknos.db.orm import DocumentEmbedding
from iknos.provenance.action_log import record_action
from iknos.types.governance import Sensitivity
from iknos.types.nodes import Span

logger = logging.getLogger(__name__)

# Bump to deliberately invalidate prior segmentations (forces the resegmentation
# guard to treat old spans as stale). Part of the content hash, never the span id.
# v2 (G1.0): the content hash now folds in the parse front-end's content hash, so a
# re-parse with a different parser (even one yielding identical reading-order text but
# different layout) correctly invalidates downstream spans instead of silently serving
# stale layouts.
SEGMENT_SCHEMA_VERSION = 2

# Action actor for segmentation runs — the idempotency/guard discriminator, exactly
# as the propositionizer uses actor='propositionizer'.
_ACTOR = "segmenter"

# Action actor for the Stage 0 parse front-end (§1, G1.0) — gives "parse once" (§6.1)
# its enforcement point and a future content-addressed parse cache its key.
_PARSE_ACTOR = "parser"

# Sentence-ending punctuation followed by whitespace/end, OR a trailing fragment.
_SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+(?=\s|$)|$)")


class DocumentResegmentationError(Exception):
    """A document was re-segmented with content/params differing from its prior run.

    A document's segmentation identity is its input ``content_hash``; the same
    document maps to the same Span ids regardless. Silently re-writing would orphan
    the old spans and every Proposition that points at them. Re-ingest with cascade
    cleanup is a future increment (G1.7); until then this fails loud — mirrors
    ``domain.loader.PackImmutabilityError``.
    """


@dataclass(frozen=True)
class SpanPersistResult:
    """Outcome of a span-persistence call.

    ``spans`` is the persisted set (whitespace spans excluded) in document order, so
    the caller can feed it straight to ``Propositionizer.propositionize_document``.
    ``embedding_rows`` counts rows *written this call* — zero on a no-op re-run.
    """

    spans: list[Span]
    embedding_rows: int
    skipped: int  # whitespace / zero-vector spans, which carry no claims
    already_segmented: bool  # guard short-circuited; no writes were issued


def split_sentences(text: str) -> list[dict[str, Any]]:
    """Offset-preserving sentence split → ``{text, start_char, end_char}`` dicts.

    A deliberately crude regex heuristic (promoted from ``scripts/illustrate.py``) —
    a **v1 seam**; a real linguistic splitter is a separate concern. ``start_char`` /
    ``end_char`` are the raw match bounds (unstripped), so ``text[start:end].strip()``
    recovers the sentence text.
    """
    out: list[dict[str, Any]] = []
    for m in _SENTENCE_RE.finditer(text):
        if not m.group().strip():
            continue
        out.append({"text": m.group().strip(), "start_char": m.start(), "end_char": m.end()})
    return out


def span_id_for(document_id: uuid.UUID, start: int, end: int, level: int) -> uuid.UUID:
    """Deterministic Span id, namespaced under the document (cf. pack ``entity_id``).

    Permanent on-disk identity contract: the key format ``"start:end:level"`` must
    never change — doing so would orphan every persisted span.
    """
    return uuid.uuid5(document_id, f"{start}:{end}:{level}")


def span_content_hash(
    raw_text: str,
    *,
    segmenter_params: dict[str, Any],
    model: str,
    parse_content_hash: str | None = None,
) -> str:
    """SHA-256 over the segmentation **inputs** — the immutability discriminator.

    Inputs only (raw text + params + model + schema version + the upstream parse hash),
    never the *derived* char-spans or embeddings: torch/CUDA float drift in pooling must
    not spuriously trip the resegmentation guard (cf. ``DomainPack.content_hash`` hashes
    the declaration, not the computed closure).

    ``parse_content_hash`` (G1.0) folds the Stage 0 parse identity into the segmentation
    identity: two parsers yielding *identical* reading-order text but different layout
    must still re-segment (else the stale layouts are served silently). ``None`` is the
    legacy/no-parse value for direct ``persist_spans`` callers; ``ingest_document``
    always threads the real (null-parser) hash.
    """
    payload = {
        "raw_text": raw_text,
        "segmenter": segmenter_params,
        "model": model,
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "parse_content_hash": parse_content_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_zero_vector(emb: list[float]) -> bool:
    """A whitespace-only span pools to a literal zero vector (see ``pool_span``)."""
    return all(c == 0.0 for c in emb)


async def _segmented_hash(session: AsyncSession, document_id: uuid.UUID) -> str | None:
    """The ``content_hash`` of this document's most recent segmentation, or ``None``.

    Action-table backed (single source of truth), mirroring
    ``Propositionizer._extracted_hash`` (G1.7).
    """
    row = await session.execute(
        text(
            "SELECT inputs->>'content_hash' FROM actions "
            f"WHERE actor = '{_ACTOR}' AND inputs->>'document_id' = :did "
            "ORDER BY timestamp DESC LIMIT 1"
        ),
        {"did": str(document_id)},
    )
    return row.scalar_one_or_none()


async def _parsed_hash(session: AsyncSession, document_id: uuid.UUID) -> str | None:
    """The ``content_hash`` of this document's most recent parse, or ``None``.

    Action-table backed, mirroring :func:`_segmented_hash` — the single source of truth
    for "has this document been parsed, and with what inputs". Used to keep the parse
    Action idempotent (record only on a new/changed parse).
    """
    row = await session.execute(
        text(
            "SELECT inputs->>'content_hash' FROM actions "
            f"WHERE actor = '{_PARSE_ACTOR}' AND inputs->>'document_id' = :did "
            "ORDER BY timestamp DESC LIMIT 1"
        ),
        {"did": str(document_id)},
    )
    return row.scalar_one_or_none()


async def persist_spans(
    session: AsyncSession,
    document_id: uuid.UUID,
    char_spans: list[tuple[int, int]],
    embeddings: list[list[float]],
    *,
    content_hash: str,
    segmenter_params: dict[str, Any],
    model: str | None = None,
    level: int = 0,
    layouts: list[dict[str, Any] | None] | None = None,
) -> SpanPersistResult:
    """Persist segmented spans (Span vertices + dense rows) idempotently.

    Torch-free: ``embeddings`` are precomputed and positionally aligned to
    ``char_spans``. ``layouts`` (optional, same alignment) carries the parse
    front-end's ``{page, bbox}`` visual-provenance handle per span (G1.0); ``None``
    — the whole arg or any element — means plain-text ingest with no layout.
    Caller-owned transaction — does **not** commit. See module docstring for the
    immutability / atomicity model.
    """
    from iknos.db.age import cypher_map, execute_cypher

    prior = await _segmented_hash(session, document_id)
    if prior is not None and prior != content_hash:
        raise DocumentResegmentationError(
            f"document {document_id} was already segmented with different inputs "
            f"(stored hash {prior[:12]}…, declared {content_hash[:12]}…). A document's "
            f"segmentation is immutable — re-ingest with cascade cleanup is not yet "
            f"supported (G1.7)."
        )
    already = prior == content_hash

    layout_list = layouts if layouts is not None else [None] * len(char_spans)

    spans: list[Span] = []
    skipped = 0
    rows = 0
    for (start, end), emb, layout in zip(char_spans, embeddings, layout_list, strict=True):
        if _is_zero_vector(emb):
            # A whitespace span carries no claims (the propositionizer would extract
            # nothing) and a zero vector poisons cosine ANN — drop it from both stores.
            skipped += 1
            logger.warning(
                "skipping whitespace/zero-vector span doc=%s [%d:%d]", document_id, start, end
            )
            continue

        span_id = span_id_for(document_id, start, end, level)
        spans.append(
            Span(
                id=span_id,
                document_id=document_id,
                start=start,
                end=end,
                level=level,
                layout=layout,
            )
        )
        if already:
            continue

        props: dict[str, Any] = {
            "id": str(span_id),
            "document_id": str(document_id),
            "start": start,
            "end": end,
            "level": level,
            **Sensitivity().flatten(),
        }
        if layout is not None:
            # Opaque {page, bbox,...} from the parser (G1.0); cypher_map JSON-encodes it
            # as a string property, like sensitivity_compartments / pack entity_types.
            props["layout"] = layout
        # MERGE (defense-in-depth alongside the content-hash guard): id-keyed upsert,
        # so even a forced re-run cannot duplicate the vertex.
        await execute_cypher(
            session, f"MERGE (s:Span {{id: '{span_id}'}}) SET s = {cypher_map(props)}"
        )

        # Dense row: upsert on the partial unique index (migration 0005) so the row id
        # is stable across re-runs (no churn) and never duplicates. pg_insert keeps the
        # pgvector type adaptation that a raw text() bind would lose.
        stmt = pg_insert(DocumentEmbedding).values(
            document_id=document_id,
            span_id=span_id,
            span_start=start,
            span_end=end,
            level=level,
            embedding=emb,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["span_id"],
            index_where=text("span_id IS NOT NULL"),
            set_={
                "span_start": start,
                "span_end": end,
                "level": level,
                "embedding": emb,
            },
        )
        await session.execute(stmt)
        rows += 1

    if not already:
        await record_action(
            session,
            actor=_ACTOR,
            action_type="segment",
            inputs={
                "document_id": str(document_id),
                "content_hash": content_hash,
                "params": segmenter_params,
                "level": level,
                "schema_version": SEGMENT_SCHEMA_VERSION,
            },
            outputs={"span_ids": [str(s.id) for s in spans], "skipped": skipped},
            model=model,
        )

    return SpanPersistResult(
        spans=spans, embedding_rows=rows, skipped=skipped, already_segmented=already
    )


async def ingest_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    raw_text: str,
    substrate: EmbeddingSubstrate,
    segmenter: SegmentationBackbone,
    *,
    title: str | None = None,
    source_uri: str | None = None,
) -> SpanPersistResult:
    """End-to-end ingest for one document: text → spans, in one transaction.

    Sequence (caller commits once on success): upsert ``document_content`` →
    MERGE the ``:Document`` vertex → **Stage 0 parse** (record the parse Action) →
    embed (the single torch forward pass) → split → segment → pool each span →
    derive per-span ``layout`` → ``persist_spans``. ``document_content`` is written
    **before** the dense rows because of the embedding → content FK; on a raised
    resegmentation guard the caller's rollback reverts the text update *and* the parse
    Action too.

    Stage 0 (G1.0) here is the **identity/null parser**: plain text in, no page geometry,
    ``layout=None`` on every span — reproducing the pre-Stage-0 behaviour exactly while
    establishing the seam. A real parser (MinerU behind ``config.parser_base_url``) and a
    bytes-in entry point are later increments; ``raw_text`` and a real parser are never
    both passed (two sources of truth for one string).
    """
    from iknos.db.age import cypher_map, execute_cypher

    # document_content first (FK target for document_embeddings; source for
    # resolve_span_text). Idempotent upsert keyed on the document id.
    await session.execute(
        text(
            "INSERT INTO document_content (document_id, raw_text, title, source_uri) "
            "VALUES (:id, :raw, :title, :uri) "
            "ON CONFLICT (document_id) DO UPDATE SET "
            "raw_text = EXCLUDED.raw_text, title = EXCLUDED.title, source_uri = EXCLUDED.source_uri"
        ),
        {"id": document_id, "raw": raw_text, "title": title, "uri": source_uri},
    )

    doc_props: dict[str, Any] = {"id": str(document_id), **Sensitivity().flatten()}
    if title is not None:
        doc_props["title"] = title
    await execute_cypher(
        session, f"MERGE (d:Document {{id: '{document_id}'}}) SET d = {cypher_map(doc_props)}"
    )

    # Stage 0 — parse front-end. The null parser is the identity transform, so
    # ``parse_result.text == raw_text`` and char offsets / document_content are unchanged.
    parse_result = NullParser().parse_text(raw_text)
    parse_ch = parse_content_hash(
        input_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        media_type="text/plain",
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version,
        parse_schema_version=parse_result.parse_schema_version,
    )
    # Record the parse Action only when new/changed (idempotent like the segment Action).
    # On a *changed* parse the segmentation guard below raises and the caller's rollback
    # reverts this write too, so a stale re-parse aborts atomically with no partial state.
    if await _parsed_hash(session, document_id) != parse_ch:
        await record_action(
            session,
            actor=_PARSE_ACTOR,
            action_type="parse",
            inputs={
                "document_id": str(document_id),
                "content_hash": parse_ch,
                "parser_name": parse_result.parser_name,
                "parser_version": parse_result.parser_version,
                "media_type": "text/plain",
                "schema_version": parse_result.parse_schema_version,
            },
            outputs={"elements": len(parse_result.elements)},
            model=parse_result.parser_name,
        )

    context = substrate.embed_document(raw_text)
    sentences = split_sentences(raw_text)
    char_spans = segmenter.segment_document(sentences, context)
    embeddings = [context.pool_span(start, end) for start, end in char_spans]
    layouts = layouts_for_spans(char_spans, parse_result)

    segmenter_params = {
        "max_len": segmenter.max_len,
        "penalty_weight": segmenter.penalty_weight,
        "density_weight": segmenter.density_weight,
    }
    content_hash = span_content_hash(
        raw_text,
        segmenter_params=segmenter_params,
        model=substrate.model_name,
        parse_content_hash=parse_ch,
    )

    return await persist_spans(
        session,
        document_id,
        char_spans,
        embeddings,
        content_hash=content_hash,
        segmenter_params=segmenter_params,
        model=substrate.model_name,
        layouts=layouts,
    )
