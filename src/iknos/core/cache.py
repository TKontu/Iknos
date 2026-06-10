"""Content-addressed extraction cache key (Phase 1, G1.7) — the "extract once" discriminator.

The propositionizer's idempotency must skip a span only when re-running it would reproduce the
*same* extraction, and re-extract when anything that shaped the output changed — a different LLM,
a reworded prompt / schema, a different sampling regime, the verifier toggled on or off, or a
changed context window. Keying on the span id alone (the pre-G1.7 behaviour) cannot see any of
that: it would serve a stale extraction after a model upgrade. This module turns the *extraction
inputs* into a single hash that the idempotency check compares against the one stored on the span's
prior extract ``Action`` (see ``core/proposition.py``).

Deliberately **pure**: no DB, no torch, no LLM — hand-built inputs in, a hex digest out, so it is
unit-testable in isolation, exactly like ``core/consistency.py`` and ``types/epistemic.py``. The
canonicalization mirrors ``core/ingest.py::span_content_hash`` (sorted keys, compact separators,
hash the *inputs* not the derived outputs — float drift in embeddings must never trip the guard).

Unlike ``span_content_hash`` the schema version is **passed in** rather than read from a module
constant: ``EXTRACT_SCHEMA_VERSION`` lives next to the prompt it versions in ``core/proposition.py``
and importing it here would be circular (``proposition`` already imports this module). Keeping the
version a parameter leaves ``cache.py`` a dependency-free leaf.
"""

import hashlib
import json
from typing import Any


def extraction_content_hash(
    *,
    target_text: str,
    context_text: str,
    model: str,
    schema_version: int,
    sampling: dict[str, Any],
    verifier: dict[str, Any] | None,
) -> str:
    """SHA-256 over the extraction inputs — the pipeline-version discriminator.

    Args:
        target_text: the span text the extractor decomposes (the source of the claims).
        context_text: the preceding-window text shown to the model for reference resolution;
            included because changing the window changes the prompt and so can change the output.
        model: the extractor model id (``LLMClient.model``); recorded on ``Action.model`` too.
        schema_version: ``EXTRACT_SCHEMA_VERSION`` — bumped on any prompt / schema / enum change.
        sampling: the decoding regime (temperature, top_p, ``n_samples`` …); a different regime
            yields a different extraction and a different agreement signal, so it is in the key.
        verifier: ``None`` when no verifier is configured, else
            ``{"model": ..., "schema_version": ...}``; toggling or changing the verifier changes
            the derived ``faithfulness``, so it must invalidate the cache.

    Inputs only — never the derived propositions/embeddings (cf. ``span_content_hash``,
    ``DomainPack.content_hash``): non-deterministic float drift must not change the digest.
    """
    payload = {
        "target_text": target_text,
        "context_text": context_text,
        "model": model,
        "schema_version": schema_version,
        "sampling": sampling,
        "verifier": verifier,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
