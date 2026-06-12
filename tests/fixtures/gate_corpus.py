"""Typed, model-free loader for the gate corpus (``tests/fixtures/gate_corpus/``).

The gate corpus is the **Trial A0 / V1 asset** named in ``docs/todo_trials.md``: ten authored
documents (d01–d10) in a wind-turbine gearbox high-speed-shaft (HSS) bearing root-cause
investigation (architecture.md §14's running example), carrying deliberately **planted**
items — contradictions, dissimilar refuters, an overturning fact, coreference and over-merge
traps — so the Phase-4 validation gate and the A1–A7 / E1 trials run against a corpus with a
known answer key, and the corpus then doubles as the permanent regression suite.

**This module adds only what the existing schema lacks.** Document + anchor loading reuses
``corpus.load_corpus(corpus_dir=GATE_CORPUS_DIR)`` (same manifest schema, same
quote-not-offset discipline, same :class:`~corpus.Anchor.locate` primitive). On top of the
documents it parses two gate-only manifest sections:

* ``[[planted]]`` — one row per planted item: a stable ``id``, its :class:`PlantedKind`, the
  owning document, the verbatim anchor ``quote(s)``, and the cross-references that make an
  item relational (a contradiction ``pair``, a refuter→hypothesis ``refutes`` link, a
  judgement/fact→hypothesis ``supports`` link, an overturning fact→``retracts`` link).
* ``[[hypotheses]]`` — the four candidate causes (the reference set d09 enumerates) and their
  gate :class:`HypothesisRole` (the true cause, the one favoured before the overturning fact,
  and the two refuted ones). The pre/post-d10 flip is the §8 retraction measurement.

**V1 carries no gold labels** (Trial V2's scope): there is no ``gold_*`` here. The planted
table is the *inventory* the labelling and the trials are built against — not the labels.

Why this loader is pure (no torch, no DB, no network): like the Phase-1 corpus, the gate
corpus's automated guarantee is self-consistency of its *planted inventory* — every planted
quote resolves to exactly one span in its document, every cross-reference resolves, the
vocabulary is closed, and d08 provably clears one embedding window by word count. None of
that needs a model; ``tests/unit/test_gate_corpus.py`` proves it with nothing loaded.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tests.fixtures.corpus import Corpus, CorpusDocument, load_corpus

GATE_CORPUS_DIR = Path(__file__).parent / "gate_corpus"


class PlantedKind(StrEnum):
    """What a planted item exercises. Closed vocabulary — a typo in the manifest raises.

    Mirrors the gate's design targets (architecture.md §3.1/§5.1/§5.2/§9.1/§11.2, §8):
    the contradiction/refuter/overturning trio drives the retraction experiment; the
    coref/over-merge pair is the §5.2 entity-resolution case; the epistemic kinds
    (negation, hedge, attribution, the observation-vs-self-serving-judgement split) are the
    §3.1 faithfulness cases.
    """

    HARD_NEGATION = "hard_negation"
    HEDGE = "hedge"
    CONTRADICTION = "contradiction"
    GENUINE_OBSERVATION = "genuine_observation"
    SELF_SERVING_JUDGEMENT = "self_serving_judgement"
    COMPONENT_HIERARCHY = "component_hierarchy"
    DISSIMILAR_REFUTER = "dissimilar_refuter"
    ENTITY_COREF = "entity_coref"
    OVER_MERGE_TRAP = "over_merge_trap"
    ADMISSION_AGAINST_INTEREST = "admission_against_interest"
    ATTRIBUTION = "attribution"
    LOAD_BEARING_TAIL_FACT = "load_bearing_tail_fact"
    REFERENCE_HYPOTHESIS_SET = "reference_hypothesis_set"
    OVERTURNING_FACT = "overturning_fact"


class HypothesisRole(StrEnum):
    """A hypothesis's role in the gate experiment. Closed vocabulary.

    Exactly one ``true_cause`` and one ``favoured_before_overturn`` are expected — the two
    sides of the d10 hypothesis-state flip — plus the ``refuted`` hypotheses each excluded by
    a dissimilar refuter.
    """

    TRUE_CAUSE = "true_cause"
    FAVOURED_BEFORE_OVERTURN = "favoured_before_overturn"
    REFUTED = "refuted"


@dataclass(frozen=True)
class PlantedItem:
    """One planted item: a stable id, its kind, the owning document, and its anchor quote(s).

    ``quotes`` are verbatim substrings of the owning document; :meth:`locate` finds each and
    asserts it occurs **exactly once** (the same anti-drift contract as :class:`corpus.Anchor`
    — a hand-counted offset would rot on the first edit). The optional cross-references are
    set only on relational kinds:

    * ``pair`` — another planted id this contradicts (the two sides of a contradiction).
    * ``refutes`` — the hypothesis id a ``dissimilar_refuter`` excludes.
    * ``supports`` — the hypothesis id an observation / judgement / tail fact bears on.
    * ``retracts`` — the planted id an ``overturning_fact`` withdraws.
    """

    id: str
    kind: PlantedKind
    document: str
    quotes: tuple[str, ...]
    note: str
    pair: str | None = None
    refutes: str | None = None
    supports: str | None = None
    retracts: str | None = None

    def locate(self, document: CorpusDocument) -> list[tuple[int, int]]:
        """Return the unique ``[start, end)`` of each quote in ``document``; raise on 0 or >1.

        Reuses :meth:`corpus.CorpusDocument.find_unique` so the gate corpus and the Phase-1
        corpus share one definition of "an anchor identifies exactly one span".
        """
        return [document.find_unique(q) for q in self.quotes]


@dataclass(frozen=True)
class Hypothesis:
    """A candidate cause: a stable id, a human label, its gate role, and (if refuted) the
    document that excludes it."""

    id: str
    label: str
    role: HypothesisRole
    note: str
    refuted_by: str | None = None


@dataclass(frozen=True)
class GateCorpus:
    """The gate corpus: its documents (a :class:`corpus.Corpus`) plus the planted inventory
    and the hypothesis set."""

    corpus: Corpus
    planted: tuple[PlantedItem, ...]
    hypotheses: tuple[Hypothesis, ...]

    # --- document access (delegated to the shared Corpus) ---

    @property
    def documents(self) -> tuple[CorpusDocument, ...]:
        return self.corpus.documents

    def get_document(self, doc_id: str) -> CorpusDocument:
        return self.corpus.get(doc_id)

    # --- planted / hypothesis access ---

    def planted_by_kind(self, kind: PlantedKind) -> list[PlantedItem]:
        return [p for p in self.planted if p.kind == kind]

    def get_planted(self, planted_id: str) -> PlantedItem:
        for p in self.planted:
            if p.id == planted_id:
                return p
        raise KeyError(planted_id)

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        for h in self.hypotheses:
            if h.id == hypothesis_id:
                return h
        raise KeyError(hypothesis_id)

    def hypotheses_by_role(self, role: HypothesisRole) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.role == role]


def _planted_from_toml(d: dict[str, object]) -> PlantedItem:
    quotes = d["quotes"]
    if not isinstance(quotes, list) or not quotes:
        raise ValueError(f"planted item {d.get('id')!r} must list at least one quote")
    return PlantedItem(
        id=str(d["id"]),
        kind=PlantedKind(str(d["kind"])),  # raises on an unknown kind — the vocabulary cannot drift
        document=str(d["document"]),
        quotes=tuple(str(q) for q in quotes),
        note=str(d.get("note", "")),
        pair=_opt_str(d.get("pair")),
        refutes=_opt_str(d.get("refutes")),
        supports=_opt_str(d.get("supports")),
        retracts=_opt_str(d.get("retracts")),
    )


def _hypothesis_from_toml(d: dict[str, object]) -> Hypothesis:
    return Hypothesis(
        id=str(d["id"]),
        label=str(d["label"]),
        role=HypothesisRole(str(d["role"])),  # raises on an unknown role
        note=str(d.get("note", "")),
        refuted_by=_opt_str(d.get("refuted_by")),
    )


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def load_gate_corpus() -> GateCorpus:
    """Load the gate corpus: documents (via the shared loader) + planted + hypotheses.

    Pure; no torch/DB/network. The document section is loaded by
    :func:`corpus.load_corpus`; this function adds the ``[[planted]]`` and ``[[hypotheses]]``
    sections of the same manifest.
    """
    corpus = load_corpus(GATE_CORPUS_DIR)
    manifest = tomllib.loads((GATE_CORPUS_DIR / "manifest.toml").read_text(encoding="utf-8"))
    planted = tuple(_planted_from_toml(p) for p in manifest.get("planted", []))
    hypotheses = tuple(_hypothesis_from_toml(h) for h in manifest.get("hypotheses", []))
    return GateCorpus(corpus=corpus, planted=planted, hypotheses=hypotheses)
