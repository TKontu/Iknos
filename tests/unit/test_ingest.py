"""Unit tests for the pure ingest helpers (G1.9) — no DB, no torch.

Span persistence behaviour (MERGE/upsert/guard/whitespace-skip) needs a live
graph + relational store, so it is covered by
``tests/integration/test_span_persistence.py``. Here we pin only the pure,
deterministic pieces: deterministic ids, the content-hash discriminator, and the
offset-preserving sentence splitter.
"""

import uuid

from iknos.core.ingest import span_content_hash, span_id_for, split_sentences

_PARAMS = {"max_len": 10, "penalty_weight": 0.1, "density_weight": 0.5}


# --- deterministic span ids ---


def test_span_id_is_deterministic() -> None:
    doc = uuid.uuid4()
    assert span_id_for(doc, 0, 49, 0) == span_id_for(doc, 0, 49, 0)


def test_span_id_varies_by_offsets_level_and_document() -> None:
    doc = uuid.uuid4()
    base = span_id_for(doc, 0, 49, 0)
    assert span_id_for(doc, 0, 50, 0) != base  # end differs
    assert span_id_for(doc, 1, 49, 0) != base  # start differs
    assert span_id_for(doc, 0, 49, 1) != base  # level differs (multi-level additivity)
    assert span_id_for(uuid.uuid4(), 0, 49, 0) != base  # different document


# --- content hash (immutability discriminator) ---


def test_content_hash_is_deterministic() -> None:
    a = span_content_hash("hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3")
    b = span_content_hash("hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3")
    assert a == b


def test_content_hash_changes_on_text() -> None:
    a = span_content_hash("hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3")
    b = span_content_hash("hello there", segmenter_params=_PARAMS, model="BAAI/bge-m3")
    assert a != b


def test_content_hash_changes_on_params() -> None:
    a = span_content_hash("hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3")
    b = span_content_hash(
        "hello world",
        segmenter_params={**_PARAMS, "max_len": 20},
        model="BAAI/bge-m3",
    )
    assert a != b


def test_content_hash_changes_on_model() -> None:
    a = span_content_hash("hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3")
    b = span_content_hash("hello world", segmenter_params=_PARAMS, model="other/model")
    assert a != b


def test_content_hash_changes_on_parse_hash() -> None:
    # G1.0 (D): the upstream parse identity folds into the segmentation identity, so a
    # re-parse with a different parser (even on identical text) re-segments.
    a = span_content_hash(
        "hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3", parse_content_hash="A"
    )
    b = span_content_hash(
        "hello world", segmenter_params=_PARAMS, model="BAAI/bge-m3", parse_content_hash="B"
    )
    assert a != b


def test_content_hash_changes_on_windowing_policy() -> None:
    # G1.13 slice 2: a changed embedding windowing policy yields different span vectors, so it
    # must re-segment rather than silently reuse spans pooled under the old policy.
    a = span_content_hash(
        "hello world",
        segmenter_params=_PARAMS,
        model="BAAI/bge-m3",
        windowing={"overlap": 1024, "model_max_tokens": 8192, "window_token_size": 8190},
    )
    b = span_content_hash(
        "hello world",
        segmenter_params=_PARAMS,
        model="BAAI/bge-m3",
        windowing={"overlap": 512, "model_max_tokens": 8192, "window_token_size": 8190},
    )
    assert a != b


# --- sentence splitter (offset-preserving) ---


def test_split_sentences_round_trips_offsets() -> None:
    text = "First sentence. Second one! A third?\nFourth on a new line."
    sentences = split_sentences(text)
    assert len(sentences) == 4
    for s in sentences:
        # The stored offsets must recover the (stripped) sentence text.
        assert text[s["start_char"] : s["end_char"]].strip() == s["text"]


def test_split_sentences_ignores_blank_runs() -> None:
    assert split_sentences("   \n\n  ") == []


def test_split_sentences_keeps_unterminated_tail() -> None:
    # A trailing fragment with no sentence-ending punctuation is still a sentence.
    sentences = split_sentences("Done. Trailing fragment")
    assert [s["text"] for s in sentences] == ["Done.", "Trailing fragment"]
