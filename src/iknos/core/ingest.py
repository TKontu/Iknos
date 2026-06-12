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
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.embeddings import EmbeddingModelMismatchError, EmbeddingSubstrate
from iknos.core.parse import (
    NullParser,
    Parser,
    ParseResult,
    layouts_for_spans,
    parse_content_hash,
)
from iknos.core.reference_corpus import (
    ReferenceSealError,
    document_input_sha256,
    get_reference_seal,
    seal_reference_document,
    validate_sealable_tier,
)
from iknos.core.segmentation import SegmentationBackbone
from iknos.db.orm import DocumentEmbedding
from iknos.provenance.action_log import record_action
from iknos.provenance.metrics import elapsed_ms
from iknos.types.governance import Sensitivity
from iknos.types.nodes import Box, Span

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
    # Coarse-level results (G1.10), set only on the finest result returned by an ``ingest_*``
    # call when the segmenter has a multi-level policy. ``spans`` above is always the finest
    # (level-0) set — the granularity the proposition layer extracts from; coarse spans persist
    # for §5.1 coarse-to-fine pruning but are not propositionized. Empty for a single-level run
    # and for the per-level results inside ``coarse`` themselves (no nesting).
    coarse: tuple["SpanPersistResult", ...] = ()


@dataclass(frozen=True)
class ReferenceIngestResult:
    """Outcome of a reference-corpus ingest (:func:`ingest_reference_document`, G1.8).

    ``reused`` is the §6.1 amortization signal: ``True`` means the document was already
    sealed into ``box_id`` with identical content, so the whole pipeline (embed → segment
    → persist) was **skipped** and ``result`` is ``None`` (nothing was processed this
    call — the persisted spans are already in the graph for every investigation to read).
    ``False`` means this was the first ingest: ``result`` carries the fresh
    :class:`SpanPersistResult` and the document is now sealed read-only.
    """

    box_id: uuid.UUID
    reused: bool
    result: SpanPersistResult | None


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
    windowing: dict[str, Any] | None = None,
) -> str:
    """SHA-256 over the segmentation **inputs** — the immutability discriminator.

    Inputs only (raw text + params + model + schema version + the upstream parse hash +
    the embedding windowing policy), never the *derived* char-spans or embeddings:
    torch/CUDA float drift in pooling must not spuriously trip the resegmentation guard
    (cf. ``DomainPack.content_hash`` hashes the declaration, not the computed closure).

    ``parse_content_hash`` (G1.0) folds the Stage 0 parse identity into the segmentation
    identity: two parsers yielding *identical* reading-order text but different layout
    must still re-segment (else the stale layouts are served silently). ``None`` is the
    legacy/no-parse value for direct ``persist_spans`` callers; ``ingest_document``
    always threads the real (null-parser) hash.

    ``windowing`` (G1.13 slice 2) folds the embedding **windowing policy** (overlap /
    model max / window token size — :meth:`DocumentContext.windowing_policy`, *not* the
    data-dependent window count/boundaries) into the identity: a changed windowing policy
    yields different span vectors, so it must re-segment rather than silently reuse spans
    pooled under the old policy. ``None`` is the legacy/no-windowing value for direct
    callers; ``_ingest_parsed`` always threads the real policy. (Like the G1.15 cache-key
    change, the first run after this lands re-hashes existing documents — a one-time loud
    resegmentation refusal, not a silent regression.)
    """
    payload = {
        "raw_text": raw_text,
        "segmenter": segmenter_params,
        "model": model,
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "parse_content_hash": parse_content_hash,
        "windowing": windowing,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _has_no_embedding(emb: list[float] | None) -> bool:
    """True for a span that carries no usable dense vector and must be skipped.

    Two cases collapse here: ``None`` — the post-G1.17 signal from ``pool_span`` that a span
    overlapped no token (e.g. whitespace) — and a literal all-zero vector, the pre-G1.17 sentinel.
    Skipping both keeps the invariant "no zero vector reaches pgvector" even for a direct caller
    that still hands in zeros (defense in depth, review R3).
    """
    return emb is None or all(c == 0.0 for c in emb)


async def _segmented_hash(session: AsyncSession, document_id: uuid.UUID, level: int) -> str | None:
    """The ``content_hash`` of this document's latest segmentation **at this level**, or ``None``.

    Action-table backed (single source of truth), mirroring
    ``Propositionizer._extracted_hash`` (G1.7). The ``level`` filter (G1.10) makes the
    resegmentation guard per-level, so persisting a coarse level does not collide with the
    fine level's stored hash: each level has its own immutability identity. Existing
    pre-G1.10 segment Actions already recorded ``inputs.level = 0``, so the level-0 lookup
    is backward compatible. The partial index ``ix_actions_segment_document_id`` still serves
    the ``document_id`` predicate; the ``level`` equality is a cheap residual filter.
    """
    row = await session.execute(
        text(
            "SELECT inputs->>'content_hash' FROM actions "
            f"WHERE actor = '{_ACTOR}' AND inputs->>'document_id' = :did "
            "AND inputs->>'level' = :lvl "
            "ORDER BY timestamp DESC LIMIT 1"
        ),
        {"did": str(document_id), "lvl": str(level)},
    )
    return row.scalar_one_or_none()


async def _existing_embedding_model(session: AsyncSession, document_id: uuid.UUID) -> str | None:
    """The embedding model of this document's existing dense span rows, or ``None`` if none exist.

    A document's dense span index is single-space by construction (one model per ingest run), so
    one row's model is the whole document's. Lets :func:`persist_spans` refuse a model swap with a
    precise :class:`~iknos.core.embeddings.EmbeddingModelMismatchError` (G1.16) rather than the
    generic resegmentation guard (the model is also in ``span_content_hash``, so a swap trips that
    too — but the specific error names the fix: ``scripts/reembed.py``).
    """
    row = await session.execute(
        text("SELECT model FROM document_embeddings WHERE document_id = :did LIMIT 1"),
        {"did": document_id},
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
    embeddings: list[list[float] | None],
    *,
    content_hash: str,
    segmenter_params: dict[str, Any],
    model: str,
    level: int = 0,
    layouts: list[dict[str, Any] | None] | None = None,
    window_layout: dict[str, Any] | None = None,
) -> SpanPersistResult:
    """Persist segmented spans (Span vertices + dense rows) idempotently.

    Torch-free: ``embeddings`` are precomputed and positionally aligned to
    ``char_spans``. ``model`` is the embedding model that produced them — required, because
    it is the dense rows' **vector-space identity** (G1.16): it is written on every
    ``document_embeddings`` row and guards against silently mixing two embedding spaces.
    ``embeddings`` may contain ``None`` for a span that pooled to no token (``pool_span``
    returns ``None``, never a zero-vector sentinel — review R3); such spans are skipped from
    both the graph and the dense index, never written with a meaningless vector.
    ``layouts`` (optional, same alignment) carries the parse front-end's ``{page, bbox}``
    visual-provenance handle per span (G1.0); ``None`` — the whole arg or any element —
    means plain-text ingest with no layout. ``window_layout`` (optional, G1.13 slice 2) is
    the embedding window layout (count + boundaries + policy) recorded on the segment
    ``Action`` for audit; ``None`` for direct callers that don't window. Caller-owned
    transaction — does **not** commit. See module docstring for the immutability / atomicity
    model.
    """
    from iknos.db.age import cypher_map, execute_cypher

    # R12: time the whole segment-persist (guards + span/dense writes) for the segment Action's
    # duration_ms. monotonic delta; the span/whitespace counts come from the loop below.
    t0 = time.monotonic()

    # G1.16: refuse a model swap before any write. The model is also in span_content_hash, so a
    # swap trips the resegmentation guard below too — but this fires first with the specific error
    # and the actionable fix (reembed), and it is the load-bearing check for any caller whose
    # content_hash does not couple in the model.
    existing_model = await _existing_embedding_model(session, document_id)
    if existing_model is not None and existing_model != model:
        raise EmbeddingModelMismatchError(
            f"document {document_id} already has dense span rows under embedding model "
            f"{existing_model!r}, cannot mix in {model!r} (cosine across embedding spaces is "
            f"meaningless). Re-embed with scripts/reembed.py to migrate the index first."
        )

    prior = await _segmented_hash(session, document_id, level)
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
        if _has_no_embedding(emb):
            # A whitespace span carries no claims (the propositionizer would extract
            # nothing) and has no usable vector — drop it from both stores so no
            # zero/None embedding ever reaches pgvector (review R3).
            skipped += 1
            logger.warning(
                "skipping whitespace/no-embedding span doc=%s [%d:%d]", document_id, start, end
            )
            continue
        assert emb is not None  # narrowed by the _has_no_embedding skip above

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
            model=model,  # vector-space identity (G1.16)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["span_id"],
            index_where=text("span_id IS NOT NULL"),
            set_={
                "span_start": start,
                "span_end": end,
                "level": level,
                "embedding": emb,
                "model": model,
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
                # G1.13 slice 2: window count/boundaries/policy, so a windowed long-document
                # ingest is auditable (which span pooled from which window). None for callers
                # that don't window (the key is simply absent then).
                **({"windowing": window_layout} if window_layout is not None else {}),
            },
            outputs={"span_ids": [str(s.id) for s in spans], "skipped": skipped},
            model=model,
            # R12 observability floor: persisted span count + whitespace/no-embedding spans dropped
            # (review R3), with the persist wall-clock. n_spans is the set written this level.
            metrics={
                "duration_ms": elapsed_ms(t0),
                "n_spans": len(spans),
                "n_skipped_whitespace": skipped,
            },
        )

    return SpanPersistResult(
        spans=spans, embedding_rows=rows, skipped=skipped, already_segmented=already
    )


async def _ingest_parsed(
    session: AsyncSession,
    document_id: uuid.UUID,
    parse_result: ParseResult,
    *,
    parse_ch: str,
    media_type: str,
    substrate: EmbeddingSubstrate,
    segmenter: SegmentationBackbone,
    title: str | None = None,
    source_uri: str | None = None,
    parse_duration_ms: int | None = None,
) -> SpanPersistResult:
    """The Stage-0-onward tail shared by every ingest entry point (text or bytes).

    Given a :class:`~iknos.core.parse.ParseResult` and its precomputed ``parse_ch``
    (the parse-input content hash), runs the rest of ingest in one caller-owned
    transaction: upsert ``document_content`` (from ``parse_result.text``, the single
    reading-order source) → MERGE the ``:Document`` vertex → record the parse Action
    (idempotent) → embed → split → segment → pool → derive per-span ``layout`` →
    ``persist_spans``. ``document_content`` is written **before** the dense rows (the
    embedding → content FK); on a raised resegmentation guard the caller's rollback
    reverts the text update *and* the parse Action together.

    Both entry points funnel here so the parser path (real geometry) and the text path
    (null parser, ``layout=None``) cannot drift apart — the only difference between them
    is which ``ParseResult``/hash/media_type they hand in.
    """
    from iknos.db.age import cypher_map, execute_cypher

    raw_text = parse_result.text

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
                "media_type": media_type,
                "schema_version": parse_result.parse_schema_version,
            },
            outputs={"elements": len(parse_result.elements)},
            model=parse_result.parser_name,
            # R12: the parser's wall-clock, timed by the entry point (the parse runs before this
            # tail). n_spans/n_skipped_whitespace belong to the segment stage and are absent at
            # parse time, so they are omitted (the never-zero discipline) rather than written as 0.
            # Omitted whole when an entry point did not time the parse (parse_duration_ms is None).
            metrics=({"duration_ms": parse_duration_ms} if parse_duration_ms is not None else None),
        )

    context = substrate.embed_document(raw_text)
    sentences = split_sentences(raw_text)

    # Multi-level segmentation (G1.10): one cached embedding pass → spans at every configured
    # level. The level *count* is the segmenter's policy (default 2: fine + one coarse); a
    # single-level segmenter yields exactly one level here, byte-identical to the pre-G1.10
    # path. Each level persists independently (its own per-level content hash + segment Action),
    # so coarse levels are purely additive — they never trip the finest level's resegmentation
    # guard and never force a resegmentation of existing documents on deploy.
    windowing = context.windowing_policy()
    window_layout = context.window_layout()
    level_segments = segmenter.segment_document_levels(sentences, context)

    results: list[tuple[int, SpanPersistResult]] = []
    for lvl, char_spans in level_segments:
        embeddings = [context.pool_span(start, end) for start, end in char_spans]
        layouts = layouts_for_spans(char_spans, parse_result)
        params = lvl.params()
        content_hash = span_content_hash(
            raw_text,
            segmenter_params=params,
            model=substrate.model_name,
            parse_content_hash=parse_ch,
            windowing=windowing,
        )
        result = await persist_spans(
            session,
            document_id,
            char_spans,
            embeddings,
            content_hash=content_hash,
            segmenter_params=params,
            model=substrate.model_name,
            level=lvl.level,
            layouts=layouts,
            window_layout=window_layout,
        )
        results.append((lvl.level, result))

    # The finest level (lowest number) is what the proposition layer extracts from, so it is
    # the returned result; coarser levels ride along under ``.coarse`` for observability. A
    # degenerate document yields only the finest level (see segment_document_levels).
    results.sort(key=lambda r: r[0])
    finest_level, finest = results[0]
    coarse = tuple(res for level, res in results if level != finest_level)
    return replace(finest, coarse=coarse)


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
    """End-to-end ingest for a **plain-text** document: text → spans, in one transaction.

    Stage 0 here is the **identity/null parser**: plain text in, no page geometry,
    ``layout=None`` on every span — reproducing the pre-Stage-0 behaviour exactly. The null
    parser is the identity transform, so ``parse_result.text == raw_text`` and char offsets /
    ``document_content`` are unchanged. For a real document (PDF/scan) with page geometry, use
    :func:`ingest_document_bytes` with a configured parser; the two never both run on one
    document (two sources of truth for one string). The shared tail lives in
    :func:`_ingest_parsed`.
    """
    t0 = time.monotonic()
    parse_result = NullParser().parse_text(raw_text)
    parse_duration_ms = elapsed_ms(t0)
    parse_ch = parse_content_hash(
        input_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        media_type="text/plain",
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version,
        parse_schema_version=parse_result.parse_schema_version,
    )
    return await _ingest_parsed(
        session,
        document_id,
        parse_result,
        parse_ch=parse_ch,
        media_type="text/plain",
        substrate=substrate,
        segmenter=segmenter,
        title=title,
        source_uri=source_uri,
        parse_duration_ms=parse_duration_ms,
    )


async def ingest_document_bytes(
    session: AsyncSession,
    document_id: uuid.UUID,
    document_bytes: bytes,
    substrate: EmbeddingSubstrate,
    segmenter: SegmentationBackbone,
    *,
    media_type: str,
    parser: Parser,
    title: str | None = None,
    source_uri: str | None = None,
) -> SpanPersistResult:
    """End-to-end ingest for a **document file**: bytes → parse → spans, in one transaction.

    The bytes-in counterpart to :func:`ingest_document`. The injected ``parser`` (built by
    ``core/mineru.make_parser`` — a real ``MinerUParser`` when ``PARSER_BASE_URL`` is set,
    else the ``NullParser``) turns the bytes into a :class:`~iknos.core.parse.ParseResult`
    carrying reading-order text + per-element ``{page, bbox}`` geometry; ``raw_text`` is then
    *derived* from ``parse_result.text`` (never passed alongside — one source of truth). The
    parse-input hash is keyed on the **bytes digest** (``sha256(document_bytes)``), not the
    derived text, so a non-deterministic OCR re-render of the same bytes is still a cache hit.

    Everything after the parse is identical to the text path (see :func:`_ingest_parsed`),
    so spans, layouts, idempotency and the resegmentation guard behave the same — only the
    geometry differs (real regions here, ``None`` under the null parser).
    """
    t0 = time.monotonic()
    parse_result = await parser.parse(document_bytes, media_type=media_type)
    parse_duration_ms = elapsed_ms(t0)
    parse_ch = parse_content_hash(
        input_sha256=hashlib.sha256(document_bytes).hexdigest(),
        media_type=media_type,
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version,
        parse_schema_version=parse_result.parse_schema_version,
    )
    return await _ingest_parsed(
        session,
        document_id,
        parse_result,
        parse_ch=parse_ch,
        media_type=media_type,
        substrate=substrate,
        segmenter=segmenter,
        title=title,
        source_uri=source_uri,
        parse_duration_ms=parse_duration_ms,
    )


async def ingest_reference_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    raw_text: str,
    substrate: EmbeddingSubstrate,
    segmenter: SegmentationBackbone,
    *,
    box: Box,
    title: str | None = None,
    source_uri: str | None = None,
) -> ReferenceIngestResult:
    """Ingest a **reference-corpus** document **once**, read-only (§6.1 amortization, G1.8).

    The reference counterpart to :func:`ingest_document`. A reference corpus (industry
    knowledge, a domain pack's prose) is static, so §6.1 says process it **once** and reuse
    it read-only across every investigation, rather than repaying the expensive embed/
    segment/extract passes each time. This entry point makes that real:

    - **First ingest** (no seal): create ``box`` (idempotent), run the full text pipeline
      (:func:`ingest_document`'s path), then **seal** the document read-only into the box
      (``core/reference_corpus.py``). ``box`` must be ``reference``/``schema`` tier — a
      ``case``/``working`` box raises ``ValueError`` (those are the per-investigation
      regime, ingested via :func:`ingest_document`).
    - **Re-ingest, identical content** (seal matches): **skip the whole pipeline** — no
      embedding, no segmentation, no writes — and return ``reused=True``. This is the
      amortization: a later investigation pays *zero* to reuse the corpus. The short-circuit
      happens **before** ``substrate.embed_document``, so the expensive pass is never repaid.
    - **Re-ingest, changed content (or a different box)** under the same id: raise
      ``ReferenceSealError``. A reference corpus is immutable per ``(id, content)`` — bump
      the version / use a new id, exactly as a domain pack does — so entrenched reference
      knowledge can never silently drift from what dependent conclusions cited.

    Caller-owned transaction, like every ingest path: the spans **and** the seal commit
    together, so a committed seal implies committed spans (the reuse short-circuit can trust
    the seal without re-checking the graph).
    """
    from iknos.boxes.registry import create_box

    # Up-front pure guard: refuse to amortize a per-investigation (case/working) box before
    # touching the DB or the seal lookup.
    validate_sealable_tier(box.tier)

    input_sha256 = document_input_sha256(raw_text)
    seal = await get_reference_seal(session, document_id)
    if seal is not None:
        if seal.box_id != box.id:
            raise ReferenceSealError(
                f"document {document_id} is already sealed into reference box {seal.box_id}, "
                f"cannot re-seal into {box.id}. A reference document belongs to one corpus box."
            )
        if seal.input_sha256 != input_sha256:
            raise ReferenceSealError(
                f"reference document {document_id} was already sealed with different content "
                f"(stored digest {seal.input_sha256[:12]}…, declared {input_sha256[:12]}…). A "
                f"reference corpus is immutable — use a new document id or bump the corpus version."
            )
        # Identical content already sealed → §6.1 amortized reuse: skip the whole pipeline.
        return ReferenceIngestResult(box_id=box.id, reused=True, result=None)

    # First ingest: ensure the corpus box exists, run the pipeline, then seal read-only.
    await create_box(session, box)
    result = await ingest_document(
        session,
        document_id,
        raw_text,
        substrate,
        segmenter,
        title=title,
        source_uri=source_uri,
    )
    await seal_reference_document(session, document_id, box, input_sha256=input_sha256)
    return ReferenceIngestResult(box_id=box.id, reused=False, result=result)
