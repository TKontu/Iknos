"""Cross-document extraction reuse (Phase 1, G1.7b) — the "extract once" replay lookup (§6.1).

G1.7 made extraction idempotent *per span*: a span re-run under the same pipeline no-ops, a changed
pipeline fails loud. But two *different* spans carrying identical text — the same passage after a
re-segmentation, or shared boilerplate — each still paid the full LLM extraction independently. That
per-span keying is deliberate soundness, not an oversight: the G1.7 key is ``(span_id,
content_hash)``, **not** content alone, because a pure-content skip would *drop* the second span's
propositions instead of giving it its own
(see ``test_extraction_cache.py::test_identical_text_different_span_both_materialize``).

**G1.24 trade-off (decided, not a bug).** The ``content_hash`` includes the *ordered context-span
ids*, and a span's id is ``uuid5(document_id, …)`` (``core/ingest.py``) — document-namespaced. So a
span with a **non-empty context window never reuses across documents**: identical text in two
documents yields different context-span ids, hence different hashes. Only a span with an *empty*
context (a first / single-span document) reuses cross-document. That is the deliberate price of
keying cache identity on ingest identity rather than textual coincidence (a re-segmentation that
changed which spans front the window must re-key); revisiting it is a §6.1 cost decision. Reuse
*within* a document (re-segmentation, an overlapping reference corpus ingested into the same
document) is unaffected.

G1.7b closes that cost without breaking the soundness. When a never-extracted span's
``content_hash`` matches a *previously committed* extraction anywhere, we **replay** that
extraction's propositions into the new span — new ``Proposition`` nodes, new ``EVIDENCED_BY`` edges,
fresh locally-computed embeddings — skipping only the (expensive) LLM call. The ``content_hash``
already encodes the target text, the context window (text + ordered span ids), and the extractor
identity (model, rendered prompt/schema, sampling regime incl. ``n_samples`` — ``core/cache.py``;
the verifier is **not** in it since G1.22), so a match means the extractor *would* have produced the
same propositions under the same regime; replaying serves exactly that. Reused
``faithfulness``/``provisional_reasons`` are copied from the cached nodes **only when the source's
verify-stage identity matches the reusing run's verifier** (``source_verify_sig``, G1.22); otherwise
the copied score is stale, so faithfulness is reset to unassessed and the span is queued for
verify-backfill. The replay records a ``reused_from`` pointer so the audit chain stays one hop away.

This module owns the **read** half: finding a reusable extraction and reconstructing its
propositions from the graph. It is deliberately free of the embedding substrate and the write path —
the *write* half (replaying into a new span, re-embedding the text under the current model) stays in
``core/proposition.py::Propositionizer``, which holds the substrate and the shared persistence path.

Robustness: a lookup that finds a matching ``Action`` but cannot fully reconstruct its propositions
from the graph (any referenced vertex missing — e.g. deleted by a future cascade re-extract) returns
``None``, so the caller falls back to a fresh extraction rather than replaying a partial result. An
extraction that legitimately produced *zero* propositions (an empty span) is a valid reuse: it
replays as zero propositions plus an empty extract ``Action``, still skipping the LLM.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.age import parse_agtype_map, unquote_agtype
from iknos.db.cypher import CypherQuery, NodeLabel, lit_list, node
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    ProvisionalReason,
    Routing,
    decode_provisional_reasons,
    provisional_reasons_for,
)


@dataclass(frozen=True)
class CachedProposition:
    """One cached proposition's replayable content, read back from its graph vertex.

    The full set of fields a fresh extraction would have produced, *except* the per-span identity
    (a new id and a fresh embedding are minted at replay time, never copied — see the module
    docstring on why the vector is re-derived rather than reused). ``faithfulness``/``agreement``
    are ``None`` exactly when the source proposition carried no such value (no verifier configured,
    or single-sample mode); ``provisional_reasons`` (R8) is the source's quarantine-reason set,
    so a replay reproduces the source's epistemic state verbatim.
    """

    text: str
    polarity: Polarity
    modality: Modality
    attribution: Attribution
    scope: str
    epistemic_class: EpistemicClass
    routing: Routing
    faithfulness: float | None
    provisional_reasons: list[str]
    agreement: float | None


@dataclass(frozen=True)
class ReusableExtraction:
    """A committed extraction replayable into a new span sharing its identical pipeline (G1.7b).

    ``source_span_id``/``source_action_id`` identify the original extraction so the replay's
    ``reused_from`` pointer keeps the audit chain (and the original verify ``Action`` behind the
    reused faithfulness) reachable. ``propositions`` may be empty (a cached empty extraction).

    ``source_verify_sig`` (G1.22) is the verify-stage identity the source was *cleanly* verified
    under (``{model, schema_version, prompt_sha, schema_sha}``), or ``None`` if the source was never
    verified (extracted with no verifier, or its verification degraded). The replay copies the
    source's faithfulness only when this matches the reusing run's current verifier; otherwise the
    copied score is stale and the span is queued for verify-backfill (``core/proposition.py``).
    """

    source_span_id: uuid.UUID
    source_action_id: uuid.UUID
    propositions: list[CachedProposition]
    source_verify_sig: dict[str, Any] | None = None


def _reasons_from_props(props: dict[str, Any]) -> list[str]:
    """Read the cached proposition's ``provisional_reasons`` (R8), tolerant of pre-R8 nodes.

    A post-R8 node stores ``provisional_reasons`` (a JSON-string list). A node written before
    R8 has only the legacy ``provisional`` boolean: when it is ``True`` we reconstruct the
    reason set from the stored ``faithfulness`` (the only two extract-time producers were
    low-faithfulness and the G1.14 polarity twin, so a True with no faithfulness explanation is
    a twin) — never *clearing* a quarantine on replay. Absent reasons + falsy boolean → empty.

    This is *legacy* reconstruction, frozen to the pre-R8 truth: a ``True`` with **null**
    faithfulness was always a polarity twin (pre-R8 verifier-off mode left ``provisional`` null,
    never ``True``), so a null score reconstructs ``POLARITY_UNSTABLE`` — *not* G1.21's
    ``UNASSESSED_FAITHFULNESS``, which :func:`provisional_reasons_for` now derives from ``None``
    but which describes a fresh degraded ingest, not a historical node. (The replay write path
    re-folds the live faithfulness reason regardless, so a null-faithfulness replay still lands
    ``UNASSESSED_FAITHFULNESS`` on the new node — see ``_build_replay_results``.)
    """
    reasons = decode_provisional_reasons(props.get("provisional_reasons"))
    if reasons or props.get("provisional") is not True:
        return reasons
    faith = props.get("faithfulness")
    if faith is None:
        return [ProvisionalReason.POLARITY_UNSTABLE.value]
    return sorted(provisional_reasons_for(faith)) or [ProvisionalReason.POLARITY_UNSTABLE.value]


def _cached_proposition_from_props(props: dict[str, Any]) -> CachedProposition:
    """Rebuild a :class:`CachedProposition` from an AGE ``properties(p)`` map.

    Enum fields persist as plain strings (``StrEnum``) and rebuild through their constructors;
    ``faithfulness``/``agreement`` come back as JSON number or ``null`` → ``None``;
    ``provisional_reasons`` decodes via :func:`_reasons_from_props` (R8, pre-R8 tolerant).
    Mirrors how ``core/proposition.py::_write_propositions`` wrote them (``prop_props``).
    """
    return CachedProposition(
        text=props["text"],
        polarity=Polarity(props["polarity"]),
        modality=Modality(props["modality"]),
        attribution=Attribution(props["attribution"]),
        scope=props.get("scope", ""),
        epistemic_class=EpistemicClass(props["epistemic_class"]),
        routing=Routing(props["routing"]),
        faithfulness=props.get("faithfulness"),
        provisional_reasons=_reasons_from_props(props),
        agreement=props.get("agreement"),
    )


async def _source_verify_sig(session: AsyncSession, source_span_id: str) -> dict[str, Any] | None:
    """The verify-stage identity the source span was *cleanly* verified under, or ``None`` (G1.22).

    Reads ``verify_sig`` off the source span's newest verify ``Action``. That field is present only
    when the whole span verified without a degraded proposition (``proposition.py::
    _record_verify_action``), so ``None`` correctly covers both "never verified" and "verification
    degraded" — in either case the reusing run must not trust a copied faithfulness across a
    verifier change.
    """
    # Select the whole ``inputs`` jsonb column (decodes to a Python dict) rather than an
    # ``inputs->'verify_sig'`` sub-expression, whose driver typing is unspecified — the same
    # discipline as :func:`find_reusable_extraction` below.
    row = await session.execute(
        text(
            "SELECT inputs FROM actions WHERE actor = 'verifier' "
            "AND inputs->>'target_span' = :sid ORDER BY timestamp DESC LIMIT 1"
        ),
        {"sid": source_span_id},
    )
    inputs = row.scalar_one_or_none()
    if inputs is None:
        return None
    sig: dict[str, Any] | None = inputs.get("verify_sig")
    return sig


async def find_reusable_extraction(
    session: AsyncSession, content_hash: str
) -> ReusableExtraction | None:
    """The newest committed extraction matching ``content_hash``, fully reconstructed — or None.

    Backed by ``ix_actions_extract_content_hash`` (migration 0012): the newest propositionizer
    extract ``Action`` carrying this exact ``content_hash`` in its ``inputs``. ``None`` when no such
    extraction exists (a true cache miss → the caller runs the LLM) **or** when the matched
    extraction's propositions cannot all be read back from the graph (a referenced vertex is
    missing — the conservative fall-back to a fresh extraction; see the module docstring).

    The match is sound because ``content_hash`` encodes the full pipeline identity, so any matching
    extraction would have produced the same propositions under the same regime (``core/cache.py``).
    """
    # Select the whole ``outputs`` jsonb column (the SQLAlchemy/asyncpg path that decodes to a
    # Python dict — exercised across the action tests as ``rec.outputs[...]``) rather than a
    # ``outputs->'propositions'`` sub-expression, so the proposition-id list never depends on how
    # the driver types a jsonb sub-value. ``inputs->>'content_hash'``/``target_span`` stay ``->>``
    # (text), like ``_extracted_hash``.
    row = (
        await session.execute(
            text(
                "SELECT id, inputs->>'target_span', outputs FROM actions "
                "WHERE actor = 'propositionizer' AND inputs->>'content_hash' = :h "
                "ORDER BY timestamp DESC LIMIT 1"
            ),
            {"h": content_hash},
        )
    ).first()
    if row is None:
        return None
    action_id, source_span, outputs = row
    prop_ids = list(outputs.get("propositions", []))

    if not prop_ids:
        # A cached *empty* extraction (the source span yielded no factual claim). Still a valid
        # reuse — replays as zero propositions + an empty extract Action, skipping the LLM.
        return ReusableExtraction(
            source_span_id=uuid.UUID(source_span),
            source_action_id=uuid.UUID(str(action_id)),
            propositions=[],
            source_verify_sig=await _source_verify_sig(session, source_span),
        )

    # One round-trip for the whole batch; UUIDs normalized then escaped via lit_list.
    ids = [str(uuid.UUID(p)) for p in prop_ids]
    rows = await (
        CypherQuery()
        .match(node("p", NodeLabel.PROPOSITION))
        .where("p.id IN " + lit_list(ids))
        .return_("p.id, properties(p)")
        .run(session, returns="id agtype, props agtype")
    )
    by_id = {unquote_agtype(rid): parse_agtype_map(props) for rid, props in rows}

    # Conservative robustness: if any referenced proposition is no longer in the graph, do not
    # replay a partial extraction — report a miss so the caller re-extracts from scratch.
    if len(by_id) != len(prop_ids):
        return None

    # Preserve the source extraction's proposition order for a deterministic replay.
    propositions = [_cached_proposition_from_props(by_id[p]) for p in prop_ids]
    return ReusableExtraction(
        source_span_id=uuid.UUID(source_span),
        source_action_id=uuid.UUID(str(action_id)),
        propositions=propositions,
        source_verify_sig=await _source_verify_sig(session, source_span),
    )
