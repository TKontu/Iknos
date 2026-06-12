"""Self-consistency of the gate corpus (``tests/fixtures/gate_corpus/``) — the Trial V1 asset.

Model-free and DB-free, like ``test_corpus.py``: these tests keep the corpus's **planted
inventory** honest so it is a trustworthy answer key for the Phase-4 validation gate and the
A1–A7 / E1 trials. They prove the properties that make the corpus usable *without* loading a
model — every planted quote resolves to exactly one span, every cross-reference resolves, the
vocabulary is closed, the hypothesis set has the shape the d10 retraction experiment needs,
and d08 provably clears one embedding window by word count. They deliberately do **not** run
extraction or check gold labels (there are none in V1 — that is Trial V2).
"""

from collections import Counter

import pytest

from iknos.core.embeddings import MAX_MODEL_TOKENS
from tests.fixtures.gate_corpus import (
    GATE_CORPUS_DIR,
    HypothesisRole,
    PlantedKind,
    load_gate_corpus,
)

GATE = load_gate_corpus()

# The work breakdown (todo_trials.md V1) fixes the corpus shape: 10 documents, 4 hypotheses.
EXPECTED_DOCUMENT_IDS = {f"d{n:02d}" for n in range(1, 11)}
EXPECTED_HYPOTHESIS_IDS = {"H1", "H2", "H3", "H4"}


# --- the corpus loads with the expected shape ---


def test_loads_ten_documents() -> None:
    ids = {d.id for d in GATE.documents}
    assert ids == EXPECTED_DOCUMENT_IDS


def test_every_document_file_exists_and_is_nonempty() -> None:
    for d in GATE.documents:
        assert d.path.is_file(), f"missing document file: {d.path}"
        assert d.text.strip(), f"empty document: {d.id}"


def test_documents_live_under_the_gate_corpus_dir() -> None:
    # Guards against the loader silently falling back to the Phase-1 corpus directory.
    for d in GATE.documents:
        assert GATE_CORPUS_DIR in d.path.parents


def test_loads_four_hypotheses() -> None:
    assert {h.id for h in GATE.hypotheses} == EXPECTED_HYPOTHESIS_IDS


# --- every planted quote identifies exactly one span (the core V1 acceptance) ---


def test_every_planted_quote_is_unique_in_its_document() -> None:
    # locate() raises if a quote is absent or matches more than once, so each planted
    # anchor unambiguously identifies a span. Also assert the located slice round-trips.
    for item in GATE.planted:
        doc = GATE.get_document(item.document)
        for (start, end), quote in zip(item.locate(doc), item.quotes, strict=True):
            assert doc.text[start:end] == quote
            assert 0 <= start < end <= len(doc.text)


def test_planted_ids_are_unique() -> None:
    ids = [p.id for p in GATE.planted]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    assert not dupes, f"duplicate planted ids: {dupes}"


def test_every_planted_document_exists() -> None:
    for item in GATE.planted:
        assert item.document in EXPECTED_DOCUMENT_IDS, f"{item.id} -> unknown doc {item.document}"


# --- the planted vocabulary is closed and every required kind is present ---


def test_every_required_planted_kind_is_present() -> None:
    # The work breakdown names a specific planted item per document; assert none was dropped.
    present = {p.kind for p in GATE.planted}
    missing = set(PlantedKind) - present
    assert not missing, f"missing planted kinds: {sorted(k.value for k in missing)}"


# --- relational cross-references all resolve (a dangling ref is a silent corpus bug) ---


def test_contradiction_pairs_are_mutual() -> None:
    pairs = GATE.planted_by_kind(PlantedKind.CONTRADICTION)
    assert pairs, "no contradiction planted"
    for item in pairs:
        assert item.pair is not None, f"contradiction {item.id} has no pair"
        partner = GATE.get_planted(item.pair)  # raises if dangling
        assert partner.pair == item.id, f"{item.id} <-> {partner.id} not mutual"


def test_refuters_point_at_a_refuted_hypothesis() -> None:
    refuters = GATE.planted_by_kind(PlantedKind.DISSIMILAR_REFUTER)
    assert len(refuters) >= 2, "expected at least the two dissimilar refuters (H3, H4)"
    for item in refuters:
        assert item.refutes is not None, f"refuter {item.id} names no hypothesis"
        hyp = GATE.get_hypothesis(item.refutes)  # raises if dangling
        assert hyp.role is HypothesisRole.REFUTED
        assert hyp.refuted_by == item.document, f"{hyp.id}.refuted_by != {item.document}"


def test_supports_links_resolve_to_hypotheses() -> None:
    for item in GATE.planted:
        if item.supports is not None:
            GATE.get_hypothesis(item.supports)  # raises if dangling


def test_overturning_fact_retracts_a_real_planted_item() -> None:
    overturning = GATE.planted_by_kind(PlantedKind.OVERTURNING_FACT)
    assert len(overturning) == 1, "exactly one overturning fact (the d10 retraction)"
    item = overturning[0]
    assert item.retracts is not None
    retracted = GATE.get_planted(item.retracts)  # raises if dangling
    # The overturning fact withdraws one side of the contradiction pair (H2's basis).
    assert retracted.kind is PlantedKind.CONTRADICTION


# --- the hypothesis set has the shape the d10 retraction experiment needs ---


def test_hypothesis_roles_support_the_retraction_flip() -> None:
    # Exactly one true cause (H1) and one favoured-before-overturn (H2) — the two sides of
    # the d10 hypothesis-state flip — plus the refuted hypotheses.
    assert len(GATE.hypotheses_by_role(HypothesisRole.TRUE_CAUSE)) == 1
    assert len(GATE.hypotheses_by_role(HypothesisRole.FAVOURED_BEFORE_OVERTURN)) == 1
    assert len(GATE.hypotheses_by_role(HypothesisRole.REFUTED)) >= 2


def test_every_refuted_hypothesis_has_a_refuter() -> None:
    for hyp in GATE.hypotheses_by_role(HypothesisRole.REFUTED):
        assert hyp.refuted_by in EXPECTED_DOCUMENT_IDS
        refuters = [
            p for p in GATE.planted_by_kind(PlantedKind.DISSIMILAR_REFUTER) if p.refutes == hyp.id
        ]
        assert refuters, f"refuted hypothesis {hyp.id} has no planted refuter"


# --- d08: the load-bearing tail fact lives in the final tenth of a multi-window document ---


def test_d08_exceeds_one_embedding_window_by_word_count() -> None:
    d08 = GATE.get_document("d08")
    # The manifest floor must itself clear a window, and the file must meet the floor —
    # SentencePiece emits >= 1 token per whitespace word, so tokens >= words (model-free).
    assert d08.min_words > MAX_MODEL_TOKENS, "d08 floor does not guarantee a multi-window doc"
    assert d08.word_count >= d08.min_words, "d08 fell below its multi-window floor"


def test_load_bearing_fact_is_in_the_final_tenth_of_d08() -> None:
    # G1.13 tail-window coverage: the fact must sit in the document tail, so a run that drops
    # the final window loses it. Measured in words (model-free), like the window floor.
    items = GATE.planted_by_kind(PlantedKind.LOAD_BEARING_TAIL_FACT)
    assert len(items) == 1
    item = items[0]
    d08 = GATE.get_document(item.document)
    start, _ = d08.find_unique(item.quotes[0])
    words_before = len(d08.text[:start].split())
    assert words_before / d08.word_count >= 0.9, "load-bearing fact is not in the final 10%"


# --- the loader rejects a drifted manifest (the closed-vocabulary guarantee is real) ---


def test_unknown_planted_kind_raises() -> None:
    from tests.fixtures.gate_corpus import _planted_from_toml

    with pytest.raises(ValueError):
        _planted_from_toml({"id": "x", "kind": "not_a_kind", "document": "d01", "quotes": ["q"]})


def test_planted_item_with_no_quotes_raises() -> None:
    from tests.fixtures.gate_corpus import _planted_from_toml

    with pytest.raises(ValueError):
        _planted_from_toml({"id": "x", "kind": "hedge", "document": "d01", "quotes": []})
