"""Self-consistency of the Phase-1 fixture corpus (``tests/fixtures/corpus/``).

These tests are **model-free and DB-free** — they keep the corpus *labels* honest so the
corpus is a trustworthy seed for the gate corpus (Trial A5). They deliberately do **not**
re-test the embedding tiler: ``_plan_windows`` already proves the windowing mechanism in
``test_embeddings.py``. What they prove here is the property that makes the long document a
valid multi-window anchor *without* loading the model — that its whitespace-word count
clears one embedding window, which (since SentencePiece emits >= 1 token per word) forces
the production tokenization over the window boundary.
"""

from iknos.core.embeddings import MAX_MODEL_TOKENS
from iknos.types.epistemic import EpistemicClass, Polarity
from tests.fixtures.corpus import AMBIGUOUS_POLARITY, Anchor, load_corpus

CORPUS = load_corpus()


# --- the corpus loads and is internally addressable ---


def test_corpus_loads_expected_documents() -> None:
    ids = {d.id for d in CORPUS.documents}
    assert ids == {"long-case-file", "polarity-waver", "clean-baseline"}


def test_every_document_file_exists_and_is_nonempty() -> None:
    for d in CORPUS.documents:
        assert d.path.is_file(), f"missing document file: {d.path}"
        assert d.text.strip(), f"empty document: {d.id}"


def test_each_role_is_represented() -> None:
    # The two regression roles the exit criteria name, plus the happy-path baseline.
    assert CORPUS.by_role("long_multiwindow"), "no multi-window document (G1.13 anchor)"
    assert CORPUS.by_role("polarity_waver"), "no polarity-instability document (G1.14 anchor)"
    assert CORPUS.by_role("clean_baseline"), "no clean baseline document"


# --- anchors are honest: unique, in-range, correctly typed ---


def test_every_anchor_quote_is_unique_in_its_document() -> None:
    # locate() raises if the quote is absent or matches more than once, so the derived
    # [start, end) is unambiguous. Also assert the slice round-trips to the quote.
    for d in CORPUS.documents:
        for a in d.anchors:
            start, end = a.locate(d.text)
            assert d.text[start:end] == a.quote
            assert 0 <= start < end <= len(d.text)


def test_gold_labels_use_only_valid_vocabulary() -> None:
    for d in CORPUS.documents:
        for a in d.anchors:
            if a.gold_epistemic_class is not None:
                assert isinstance(a.gold_epistemic_class, EpistemicClass)
            if a.gold_polarity is not None:
                # Either a real Polarity member or the explicit waver sentinel — nothing else.
                assert (
                    isinstance(a.gold_polarity, Polarity) or a.gold_polarity == AMBIGUOUS_POLARITY
                )


def test_each_anchor_pins_exactly_one_dimension() -> None:
    # An anchor that set both an epistemic class and a polarity would be ambiguous about
    # what it regression-tests; the loader/manifest keep them one-dimensional.
    for d in CORPUS.documents:
        for a in d.anchors:
            pinned = [a.gold_epistemic_class is not None, a.gold_polarity is not None]
            assert sum(pinned) == 1, f"anchor pins {sum(pinned)} dimensions: {a.quote!r}"


# --- G1.13: the long document provably exceeds one embedding window, model-free ---


def test_long_document_exceeds_one_embedding_window_by_word_count() -> None:
    doc = CORPUS.get("long-case-file")
    # The manifest floor must itself clear a window, and the file must meet the floor.
    assert doc.min_words > MAX_MODEL_TOKENS, "floor does not guarantee a multi-window document"
    assert doc.word_count >= doc.min_words, (
        f"long document shrank below its floor: {doc.word_count} < {doc.min_words}"
    )
    # tokens >= words (SentencePiece never emits < 1 token per whitespace word), so a word
    # count above MAX_MODEL_TOKENS forces the production tokenization past the window edge.
    assert doc.word_count > MAX_MODEL_TOKENS


def test_short_documents_fit_one_window() -> None:
    # The waver and baseline anchors live in short documents on purpose — they isolate the
    # polarity / routing behaviour from the windowing path.
    for role in ("polarity_waver", "clean_baseline"):
        for d in CORPUS.by_role(role):
            assert d.word_count <= MAX_MODEL_TOKENS


# --- G1.14: the polarity-waver anchor is present and correctly marked ---


def test_polarity_waver_anchor_is_present() -> None:
    docs = CORPUS.by_role("polarity_waver")
    wavers: list[Anchor] = [a for d in docs for a in d.anchors if a.is_polarity_waver]
    assert wavers, "no AMBIGUOUS_POLARITY anchor — G1.14 regression input is missing"
    for a in wavers:
        # A waver is a negative confidence signal, not a definite polarity.
        assert a.gold_polarity == AMBIGUOUS_POLARITY
        assert not isinstance(a.gold_polarity, Polarity)


# --- G1.2: both routing classes are represented for the routing regression ---


def test_routing_anchors_cover_observation_and_judgement() -> None:
    classes = {
        a.gold_epistemic_class
        for d in CORPUS.documents
        for a in d.anchors
        if a.gold_epistemic_class is not None
    }
    assert EpistemicClass.OBSERVATION in classes
    assert EpistemicClass.JUDGEMENT in classes
