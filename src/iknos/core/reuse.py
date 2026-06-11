"""Cross-document extraction reuse (Phase 1, G1.7b) — the "extract once" replay lookup (§6.1).

G1.7 made extraction idempotent *per span*: a span re-run under the same pipeline no-ops, a changed
pipeline fails loud. But two *different* spans carrying identical text — the same passage after a
re-segmentation, boilerplate shared across documents, or a reference corpus overlapping a case file
— each still paid the full LLM extraction independently. That per-span keying is deliberate
soundness, not an oversight: the G1.7 key is ``(span_id, content_hash)``, **not** content alone,
because a pure-content skip would *drop* the second span's propositions instead of giving it its own
(see ``test_extraction_cache.py::test_identical_text_different_span_both_materialize``).

G1.7b closes that cost without breaking the soundness. When a never-extracted span's
``content_hash`` matches a *previously committed* extraction anywhere, we **replay** that
extraction's propositions into the new span — new ``Proposition`` nodes, new ``EVIDENCED_BY`` edges,
fresh locally-computed embeddings — skipping only the (expensive) LLM call. The ``content_hash``
already encodes the target text, the context window, and the full pipeline identity (model,
rendered prompt/schema, sampling regime incl. ``n_samples``, and verifier signature —
``core/cache.py``), so a match means the extractor *would* have produced the same propositions under
the same regime; replaying serves exactly that. Reused ``faithfulness``/``provisional`` are copied
from the cached nodes (the verifier signature is in the ``content_hash``, so re-verifying would
reproduce them), and the replay records a ``reused_from`` pointer so the audit chain to the original
verify ``Action`` is one hop away.

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

from iknos.db.age import execute_cypher, parse_agtype_map, unquote_agtype
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
)


@dataclass(frozen=True)
class CachedProposition:
    """One cached proposition's replayable content, read back from its graph vertex.

    The full set of fields a fresh extraction would have produced, *except* the per-span identity
    (a new id and a fresh embedding are minted at replay time, never copied — see the module
    docstring on why the vector is re-derived rather than reused). ``faithfulness``/
    ``provisional``/``agreement`` are ``None`` exactly when the source proposition carried no such
    value (no verifier configured, or single-sample mode), so a replay reproduces the source's
    epistemic state verbatim.
    """

    text: str
    polarity: Polarity
    modality: Modality
    attribution: Attribution
    scope: str
    epistemic_class: EpistemicClass
    routing: Routing
    faithfulness: float | None
    provisional: bool | None
    agreement: float | None


@dataclass(frozen=True)
class ReusableExtraction:
    """A committed extraction replayable into a new span sharing its identical pipeline (G1.7b).

    ``source_span_id``/``source_action_id`` identify the original extraction so the replay's
    ``reused_from`` pointer keeps the audit chain (and the original verify ``Action`` behind the
    reused faithfulness) reachable. ``propositions`` may be empty (a cached empty extraction).
    """

    source_span_id: uuid.UUID
    source_action_id: uuid.UUID
    propositions: list[CachedProposition]


def _cached_proposition_from_props(props: dict[str, Any]) -> CachedProposition:
    """Rebuild a :class:`CachedProposition` from an AGE ``properties(p)`` map.

    Enum fields persist as plain strings (``StrEnum``) and rebuild through their constructors;
    ``faithfulness``/``provisional``/``agreement`` come back as JSON number/bool or ``null`` →
    ``None``. Mirrors how ``core/proposition.py::_persist`` wrote them (``prop_props``).
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
        provisional=props.get("provisional"),
        agreement=props.get("agreement"),
    )


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
        )

    # UUIDs are a safe charset to inline into the Cypher body (no escaping); mirrors
    # core/reembed.py::_proposition_texts. One round-trip for the whole batch.
    id_list = ", ".join(f"'{uuid.UUID(p)}'" for p in prop_ids)
    rows = await execute_cypher(
        session,
        f"MATCH (p:Proposition) WHERE p.id IN [{id_list}] RETURN p.id, properties(p)",
        returns="id agtype, props agtype",
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
    )
