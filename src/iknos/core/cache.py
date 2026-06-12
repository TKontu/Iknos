"""Content-addressed extraction cache key (Phase 1, G1.7) — the "extract once" discriminator.

The propositionizer's idempotency must skip a span only when re-running it would reproduce the
*same* extraction, and re-extract when anything that shaped the output changed — a different LLM,
a reworded prompt / schema, a different sampling regime, or a changed context window (its text
*and* which spans produced it). Keying on the span id alone (the pre-G1.7 behaviour) cannot see any
of that: it would serve a stale extraction after a model upgrade. This module turns the *extraction
inputs* into a single hash that the idempotency check compares against the one stored on the span's
prior extract ``Action`` (see ``core/proposition.py``).

G1.22: the **verifier is deliberately *not* an extraction input** — the extractor's output does not
depend on it, so folding the verifier signature into this key (the pre-G1.22 behaviour) meant a
verifier toggle/upgrade tripped a full re-extraction instead of a cheap re-verify. Verification is
now keyed as its own per-proposition stage (``core/proposition.py``: the verify ``Action``'s
``verify_sig``), so a verifier change drives **verify-backfill**, never re-extraction.

G1.24: the ordered context ``span_id``s are an input alongside the rendered ``context_text`` — a
re-segmentation that changes *which* K spans form the context window must re-key even if the
concatenated text looks similar, so the cache identity is deterministic on ingest identity rather
than on textual coincidence.

Deliberately **pure**: no DB, no torch, no LLM — hand-built inputs in, a hex digest out, so it is
unit-testable in isolation, exactly like ``core/consistency.py`` and ``types/epistemic.py``. The
canonicalization mirrors ``core/ingest.py::span_content_hash`` (sorted keys, compact separators,
hash the *inputs* not the derived outputs — float drift in embeddings must never trip the guard).

Unlike ``span_content_hash`` the schema version is **passed in** rather than read from a module
constant: ``EXTRACT_SCHEMA_VERSION`` lives next to the prompt it versions in ``core/proposition.py``
and importing it here would be circular (``proposition`` already imports this module). Keeping the
version a parameter leaves ``cache.py`` a dependency-free leaf.

G1.15 (review A4): invalidation no longer rides on the hand-bumped ``schema_version`` alone — the
*actual* rendered prompt (``prompt_sha``) and guided-decode schema (``schema_sha``) are hashed into
the key, so a prompt edit that forgot the bump still re-extracts. ``cache.py`` stays the
dependency-free hashing leaf: the SHAs are computed by the prompt's owners (``proposition.py`` /
``verify.py``) and passed in, exactly like ``schema_version``. The two pure helpers below
(:func:`sha256_hex`, :func:`canonical_json_sha256`) are the shared primitives those owners use, so
the canonicalization rule lives in one place.
"""

import hashlib
import json
from typing import Any


def sha256_hex(data: str) -> str:
    """SHA-256 hex digest of a UTF-8 string — the project's one hashing primitive."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def canonical_json_sha256(obj: Any) -> str:
    """SHA-256 of an object's **canonical** JSON (sorted keys, compact separators).

    Order-insensitive by construction: ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` hash identically,
    so re-ordering schema keys never spuriously invalidates the cache (a G1.15 requirement). The
    same canonicalization the content-hash payloads use, factored out so prompt/schema digests and
    the cache key agree on the rule.
    """
    return sha256_hex(json.dumps(obj, sort_keys=True, separators=(",", ":")))


def extraction_content_hash(
    *,
    target_text: str,
    context_text: str,
    context_span_ids: list[str],
    model: str,
    schema_version: int,
    prompt_sha: str,
    schema_sha: str,
    sampling: dict[str, Any],
) -> str:
    """SHA-256 over the extraction inputs — the pipeline-version discriminator.

    Args:
        target_text: the span text the extractor decomposes (the source of the claims).
        context_text: the preceding-window text shown to the model for reference resolution;
            included because changing the window changes the prompt and so can change the output.
        context_span_ids: the **ordered** ids of the spans that produced ``context_text`` (G1.24).
            The rendered text alone cannot distinguish a re-segmentation that changed *which* spans
            front the window from one that did not — two different span sets can render
            textually-similar context — so the span identity is keyed explicitly: cache identity is
            deterministic on ingest identity, not on textual coincidence. Order matters (the window
            is a sequence), so it is kept as a list, not a set.
        model: the extractor model id (``LLMClient.model``); recorded on ``Action.model`` too.
        schema_version: ``EXTRACT_SCHEMA_VERSION`` — a *semantic* version of the output shape.
            Since G1.15 it no longer carries invalidation alone (``prompt_sha``/``schema_sha`` do);
            it stays in the key so a deliberate shape bump still re-extracts even if the rendered
            strings happen to collide.
        prompt_sha: SHA-256 of the *rendered* extractor prompt scaffold (``proposition.py``). The
            G1.15 closure: a reworded prompt moves this digest, so a forgotten ``schema_version``
            bump can no longer silently serve a stale extraction.
        schema_sha: SHA-256 of the canonical guided-decode schema (``proposition.py``); a changed
            output schema invalidates even without a version bump. Key-order-insensitive.
        sampling: the decoding regime (temperature, top_p, ``n_samples`` …); a different regime
            yields a different extraction and a different agreement signal, so it is in the key.

    The verifier signature is **not** an input (G1.22): the extractor's output is independent of the
    verifier, so verification is keyed as its own stage (``core/proposition.py``) and a verifier
    change drives verify-backfill, not re-extraction.

    Inputs only — never the derived propositions/embeddings (cf. ``span_content_hash``,
    ``DomainPack.content_hash``): non-deterministic float drift must not change the digest.
    """
    payload = {
        "target_text": target_text,
        "context_text": context_text,
        "context_span_ids": context_span_ids,
        "model": model,
        "schema_version": schema_version,
        "prompt_sha": prompt_sha,
        "schema_sha": schema_sha,
        "sampling": sampling,
    }
    return canonical_json_sha256(payload)
