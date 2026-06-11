# Phase-1 fixture corpus

A small set of real documents plus machine-readable regression anchors. It is the
**seed for the gate corpus** named in the Phase-1 exit criteria
(`docs/todo_phase_1_ingest.md`) and in the Trial A5 faithfulness-gate metric
(`docs/todo_trials.md`). Loaded by `tests/fixtures/corpus.py`; kept honest by
`tests/unit/test_corpus.py` (model-free, DB-free).

## Layout

| file | role | anchors |
|------|------|---------|
| `documents/long_case_file.txt` | `long_multiwindow` | 1 observation + 1 judgement (G1.2) |
| `documents/polarity_waver.txt` | `polarity_waver` | 1 ambiguous-polarity span (G1.14) |
| `documents/clean_baseline.txt` | `clean_baseline` | 1 observation (control case) |
| `manifest.toml` | — | declares the documents and their anchors |

## What each document anchors

- **Long document (G1.13 — windowed embedding).** It exceeds one embedding window
  (8192 tokens). The guarantee is *model-free and CI-provable*: SentencePiece emits
  ≥ 1 token per whitespace word, so `tokens ≥ words`; the manifest's `min_words = 8200`
  is above `MAX_MODEL_TOKENS`, and the unit test asserts the file meets that floor — so
  the production tokenization provably crosses the window boundary without loading the
  model. The judgement anchor sits in the tail section, so a model-backed run also
  exercises tail-window coverage (no span silently dropped past the old truncation
  cutoff). One paragraph per line keeps anchor quotes single-line substrings.

- **Polarity-waver document (G1.14).** Hosts one span the extractor is known to waver
  on — a double negation under an epistemic hedge ("could not exclude … had not
  authorised"). Its gold polarity is the sentinel `"ambiguous"` (not a `Polarity`
  member): a correct pipeline must produce *split* polarity clusters (never agreement
  1.0) and a `provisional` proposition via twin quarantine.

- **Clean baseline.** A short, categorical, unambiguous observation — the stable-extraction
  control against which the waver behaviour is contrasted.

## Anchors carry quotes, not offsets

Each anchor stores the verbatim `quote`. The loader's `Anchor.locate(text)` finds it and
asserts it occurs **exactly once**, deriving the `[start, end)` — a hand-counted offset
would rot on the first edit. This mirrors the anti-drift discipline in `core/parse.py`.

## Scope of the automated tests

`tests/unit/test_corpus.py` proves the *labels* are honest (quotes unique and in range,
gold vocabulary valid, one dimension per anchor) and that the long document clears a
window by word count. It deliberately does **not** re-test the windowing tiler — that is
`_plan_windows` in `tests/unit/test_embeddings.py` — and it does not load a model: the
whole suite mocks the embedding substrate, so nothing downloads `BAAI/bge-m3` in CI.

The **model-backed** end-to-end run (ingest these documents through the real substrate +
LLM, assert multi-window coverage and `provisional` on the waver span, and compute the
faithfulness-gate metric) is Trial A5. This corpus is the labelled input it consumes.

## Extending the corpus

Add a document under `documents/`, register it in `manifest.toml` with a `role` and any
anchors (quote + exactly one gold dimension), and the loader + unit tests pick it up.
Keep documents one-paragraph-per-line so anchor quotes stay single-line. The documents
are fictional; any resemblance to real entities is coincidental.
