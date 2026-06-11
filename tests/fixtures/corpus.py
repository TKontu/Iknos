"""Typed, model-free loader for the Phase-1 fixture corpus (``tests/fixtures/corpus/``).

The corpus is the **seed for the gate corpus** (exit criterion of ``todo_phase_1_ingest.md``
and the Trial A5 faithfulness-gate metric, ``todo_trials.md``). It holds a small set of
real documents plus machine-readable regression anchors, so the long-document path
(G1.13) and the polarity-instability path (G1.14) have version-controlled, labelled
inputs the moment a model-backed run (Trial A5 / a manual integration run) needs them.

**Why this loader is pure (no torch, no DB, no network).** The whole test suite mocks the
embedding model — nothing downloads ``BAAI/bge-m3`` in CI — so the corpus's automated
guarantees are *model-free*: self-consistency of the labels (``tests/unit/test_corpus.py``)
and the long-document size floor. The windowing *mechanism* is proven separately and
model-free by ``_plan_windows`` in ``tests/unit/test_embeddings.py``; this corpus supplies
the real long *document*, not a re-test of the tiler.

**Anchors carry quotes, not offsets.** A hand-counted character offset into an 8000-word
document rots the moment the text is edited. Instead each anchor stores the exact ``quote``
and :meth:`Anchor.locate` finds it at load time, asserting it occurs **exactly once** (so
the offset is unambiguous). The (start, end) a consumer needs is derived, never authored —
the same anti-drift discipline ``core/parse.py`` applies to parser offsets.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

# Gold labels are validated against the real engine enums so the corpus cannot drift from
# the contract it anchors. Imported lazily-safe (these are pure StrEnums, no DB/torch).
from iknos.types.epistemic import EpistemicClass, Polarity

_CORPUS_DIR = Path(__file__).parent / "corpus"
_MANIFEST = _CORPUS_DIR / "manifest.toml"

# Sentinel gold value for a span whose polarity is *deliberately* unstable — the G1.14
# anchor. It is NOT a ``Polarity`` member (the engine never persists "ambiguous"); it is a
# corpus-level assertion that multi-sample extraction *should* split across polarities and
# the proposition *should* end up ``provisional``. Kept distinct from the real enum so a
# typo in a definite gold polarity is still caught.
AMBIGUOUS_POLARITY = "ambiguous"


@dataclass(frozen=True)
class Anchor:
    """One labelled span in a corpus document — a regression anchor for a specific gap.

    ``quote`` is verbatim text that must occur exactly once in the owning document.
    Exactly one gold field is set, identifying what the anchor pins:
      * ``gold_epistemic_class`` — G1.2 routing (observation→fact vs judgement→claim).
      * ``gold_polarity`` — G1.14; the value :data:`AMBIGUOUS_POLARITY` marks a span the
        extractor is *known to waver on*, whose propositions must come out ``provisional``.
    """

    quote: str
    note: str
    gold_epistemic_class: EpistemicClass | None = None
    gold_polarity: Polarity | str | None = None

    @property
    def is_polarity_waver(self) -> bool:
        return self.gold_polarity == AMBIGUOUS_POLARITY

    def locate(self, text: str) -> tuple[int, int]:
        """Return the unique ``[start, end)`` of ``quote`` in ``text``; raise if 0 or >1 hits.

        Uniqueness is the contract that makes the offset meaningful — an anchor that
        matches twice does not identify a span. Both failure modes are loud.
        """
        first = text.find(self.quote)
        if first == -1:
            raise ValueError(f"anchor quote not found in document: {self.quote!r}")
        if text.find(self.quote, first + 1) != -1:
            raise ValueError(f"anchor quote is not unique in document: {self.quote!r}")
        return (first, first + len(self.quote))


@dataclass(frozen=True)
class CorpusDocument:
    """One corpus document: its bytes-on-disk text plus its labelled anchors.

    ``role`` is the regression role:
      * ``"long_multiwindow"`` — exceeds one embedding window (G1.13). ``min_words`` is the
        floor that *guarantees* it: SentencePiece emits ≥ 1 token per whitespace word, so
        ``tokens ≥ words``; ``min_words > MAX_MODEL_TOKENS`` ⇒ the document provably spans
        > 1 window under the production model, with no model in the loop.
      * ``"polarity_waver"`` — hosts a :data:`AMBIGUOUS_POLARITY` anchor (G1.14).
      * ``"clean_baseline"`` — short, unambiguous happy-path document.
    """

    id: str
    path: Path
    role: str
    title: str
    media_type: str
    min_words: int
    anchors: tuple[Anchor, ...]

    @cached_property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @cached_property
    def word_count(self) -> int:
        return len(self.text.split())

    def anchors_by_role(self) -> dict[str, list[Anchor]]:
        out: dict[str, list[Anchor]] = {"epistemic": [], "polarity": []}
        for a in self.anchors:
            if a.gold_epistemic_class is not None:
                out["epistemic"].append(a)
            if a.gold_polarity is not None:
                out["polarity"].append(a)
        return out


@dataclass(frozen=True)
class Corpus:
    documents: tuple[CorpusDocument, ...]

    def get(self, doc_id: str) -> CorpusDocument:
        for d in self.documents:
            if d.id == doc_id:
                return d
        raise KeyError(doc_id)

    def by_role(self, role: str) -> list[CorpusDocument]:
        return [d for d in self.documents if d.role == role]


def _parse_gold_polarity(raw: str) -> Polarity | str:
    if raw == AMBIGUOUS_POLARITY:
        return AMBIGUOUS_POLARITY
    return Polarity(raw)  # raises on an unknown definite polarity — labels can't drift


def _anchor_from_toml(d: dict[str, Any]) -> Anchor:
    ec = d.get("gold_epistemic_class")
    pol = d.get("gold_polarity")
    return Anchor(
        quote=d["quote"],
        note=d.get("note", ""),
        gold_epistemic_class=EpistemicClass(ec) if ec is not None else None,
        gold_polarity=_parse_gold_polarity(pol) if pol is not None else None,
    )


def load_corpus() -> Corpus:
    """Load the manifest + document texts into typed objects. Pure; no torch/DB/network."""
    manifest = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    docs: list[CorpusDocument] = []
    for entry in manifest["documents"]:
        docs.append(
            CorpusDocument(
                id=entry["id"],
                path=_CORPUS_DIR / entry["filename"],
                role=entry["role"],
                title=entry["title"],
                media_type=entry.get("media_type", "text/plain"),
                min_words=entry.get("min_words", 0),
                anchors=tuple(_anchor_from_toml(a) for a in entry.get("anchors", [])),
            )
        )
    return Corpus(documents=tuple(docs))
