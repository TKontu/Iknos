# Labelling instructions (Trial V2) — please read before you start

Thank you for labelling this small set of documents. You are creating the **answer key** a
software system will later be measured against, so careful, honest labels matter more than fast
ones. You do **not** need any special background — everything is explained below in plain terms,
with two worked examples for each kind of label.

## The golden rule: do not read the answer key

There are two files in this project you must **not** open while you label:

- `tests/fixtures/gate_corpus/README.md`
- `tests/fixtures/gate_corpus/manifest.toml`

They describe the planted answers, and reading them would spoil your independent judgement. Work
only from:

- the documents in `tests/fixtures/gate_corpus/documents/` (ten plain-text files, `d01`–`d10`),
- the label files in this folder, which you fill in.

If a second person is also labelling, the two of you must work **independently** — do not
discuss the documents until both of you have finished. (Your agreement is itself one of the
measurements.)

## How you refer to text: quote it

Whenever a label points at a piece of a document, copy the **exact words** (a short verbatim
quote) and the document id (like `d03`). Do not use line numbers or your own paraphrase — a quote
is unambiguous and survives edits. Keep quotes short but long enough to be unique in that file.

## The five kinds of label

You will fill in five files, one per kind of label. Each file has a comment block at the top
describing its columns and a fictional example row to delete. Here is what each one asks for, in
plain terms.

### 1. Evidence → hypothesis links (`gold_edges.toml`)

The documents investigate why something failed; there are four candidate explanations
("hypotheses", listed in the file). For each piece of evidence that bears on an explanation, record
whether it **supports** it (makes it more likely) or **refutes** it (makes it less likely), and
quote the evidence.

- *Worked example A.* A document says "the tank was found empty." If one explanation is "the
  machine ran out of fuel," this evidence **supports** it → sign = `support`.
- *Worked example B.* A document says "the inspection found the safety guard correctly fitted." If
  one explanation is "the guard was missing," this evidence **refutes** it → sign = `refute`.
  Note that the evidence can refute an explanation **without using its words** — "correctly
  fitted" refutes "missing" even though the word "missing" never appears. Those indirect refutals
  are especially important to catch.

### 2. Which explanation was favoured, before and after (`gold_hypothesis_states.toml`)

One of the documents is a later correction that changes the picture. For each explanation, record
how believable it is **before** that correction and **after** it, using a simple four-step scale:
`false`, `implausible`, `plausible`, `true`.

- *Worked example A.* Early documents suggest the machine was overloaded, so "overload" is
  `plausible` before. A later survey shows the load was normal, so after, "overload" is `false`.
- *Worked example B.* An explanation that the evidence supports from the start and never
  contradicts stays `plausible` or `true` both before and after — record both columns even when
  they are the same.

### 3. How a statement is phrased (`gold_faithfulness.toml`)

For a sample of individual statements (quote each), record four simple things about **how it is
said** — not whether it is true:

- **Affirm or deny** — does the statement say something happened, or that it did **not**?
  (`affirmed` / `denied`). "No alarm sounded" is `denied`.
- **How sure** — is it stated as definite, or hedged? (`definite` / `probable` / `possible`).
  "It may have started at the seal" is `possible`.
- **Who says it** — the document itself, or a named person being quoted? (`document` /
  `named_person`). "The operator said it was noisy" is `named_person`.
- **What kind of statement** — something seen or measured (`observation`), something a person
  reported (`testimony`), or someone's opinion or conclusion (`judgement`).

- *Worked example A.* "The sample weighed 4.2 kg." → affirmed, definite, document, observation.
- *Worked example B.* "In our view the damage was caused by misuse." → affirmed, definite (they
  state it plainly), document, **judgement** (it is an opinion/conclusion, not a measurement).

### 4. Which mentions are the same thing (`gold_entity_clusters.toml`)

The documents refer to the same physical parts by different names ("the pump," "pump 2," "it").
Group the mentions that refer to the **same** real thing by giving them the same `cluster` label
(any short name you choose, e.g. `cluster_A`). Mentions of **different** things get **different**
cluster labels — even when the names look similar.

- *Worked example A.* "the main pump," "pump 2," and "it" (where "it" clearly means that pump) all
  get `cluster_A` — same thing, three names.
- *Worked example B.* "pump 2" and "pump 3" get **different** clusters — they are two different
  pumps, even though the names are nearly identical. Watch for this trap.

### 5. How specific each fact is (`gold_levels.toml`) — both annotators do this one

Things have parts: a machine contains a shaft, the shaft carries a bearing, the bearing contains
rollers. For each fact (quote it), record the **most specific part** it is really about, by depth:
`1` = the whole machine, `2` = a major sub-assembly, `3` = a component, `4` = a piece of a
component. Use the reference/manual document to see how the parts nest.

- *Worked example A.* "The gearbox failed." → about the whole machine → level `1`.
- *Worked example B.* "The roller surface was pitted." → about a piece inside a component → level
  `4`.

This is the one label **both** annotators always fill in separately, because we measure how well
two people agree on it.

## When you are unsure

If you genuinely cannot decide, pick the closest option and add a short `note`. Do not guess
wildly and do not look at the answer key. "Unsure, leaning X" with a note is far more useful than
a confident wrong label. When both annotators are done, a third pass reconciles any disagreements
into an agreed `consensus` value, keeping both originals.
