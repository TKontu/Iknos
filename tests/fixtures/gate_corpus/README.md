# Gate corpus — planted-contradiction evaluation corpus (Trial A0 / V1)

> ## ⚠ SPOILER — THIS IS THE ANSWER KEY
> This file and `manifest.toml` name the planted items and the **true cause**. A Trial V2
> annotator labels the documents **before** reading either of them (see "Contamination
> rule" below). The same rule binds the Trial V6 expert and the E1 baseline operators.

The **Trial A0 / V1 asset** named in `docs/todo_trials.md`: ten authored plain-text
documents in a single wind-turbine gearbox high-speed-shaft (HSS) bearing root-cause
investigation, with deliberately **planted** contradictions, dissimilar refuters, an
overturning fact, and entity-resolution traps. It gives the Phase-4 validation gate and the
A1–A7 / E1 trials a corpus with a **known answer key**, and then becomes the permanent
regression suite. Domain is architecture.md §14's running example (gearbox ⊃ high-speed
shaft ⊃ bearing ⊃ rollers).

Loaded by `tests/fixtures/gate_corpus.py` (which reuses the Phase-1 loader
`tests/fixtures/corpus.py`); kept honest by `tests/unit/test_gate_corpus.py` (model-free,
DB-free). The documents are fictional; any resemblance to real entities is coincidental.

## The scenario

Turbine **WTG-14** (gearbox asset **CB-GBX-03**) at the fictional **Cairn Brae Wind Farm**
trips on high vibration on **18 February 2024**; the drive-end high-speed-shaft bearing is
found destroyed. Four candidate causes are investigated. The evidence first points one way,
then a record is found to be in error and the conclusion flips — that flip is the §8
retraction measurement the gate exists to test.

| id  | document | role in the investigation |
|-----|----------|---------------------------|
| d01 | incident report | the initiating event; a hard negation + a hedge; one side of contradiction #1 |
| d02 | maintenance log | the other side of contradiction #1 (a "recent replacement" — later overturned) |
| d03 | supplier analysis | genuine observations **+** a self-serving judgement (§9.1) blaming the installer |
| d04 | OEM manual excerpt | reference tier; the part-whole hierarchy + the bearing-3 vs bearing-4 authority |
| d05 | vibration & duty survey | **dissimilar refuter #1** — rules out overload without naming load |
| d06 | operator interviews | coreference + an over-merge trap; an admission against interest; attribution |
| d07 | metallurgy report | **dissimilar refuter #2** — rules out a counterfeit part without its vocabulary |
| d08 | purchasing records | **> one embedding window**; a load-bearing fact buried in the final 10% |
| d09 | industry bulletin | reference tier; the four-mode reference hypothesis set (§11.2) |
| d10 | follow-up correction | **the overturning fact** (30 May 2024) — retracts the d02 replacement record |

## The hypotheses and the flip

| id | hypothesis | status | how |
|----|-----------|--------|-----|
| **H1** | Lubrication failure | **true cause** | overdue oil change (d06), degraded oil (d06), substitute under-viscosity oil grade (d08 tail) vs the VG 320 spec (d04). **Leads only after d10.** |
| **H2** | Installation / mounting error | **favoured before d10** | the d02 "recent replacement" + the supplier's self-serving judgement (d03). **Collapses at d10.** |
| **H3** | Counterfeit / non-conforming part | **refuted by d07** | metallurgy shows correct, in-spec material — no part defect |
| **H4** | Overload / transient torque | **refuted by d05** | duty survey shows torque within the normal envelope, no excursions |

**The measurement.** Before d10, the maintenance log (d02) makes it look as though the
bearing had been replaced three months earlier, so an *installation error* (H2) is the
leading explanation — reinforced by the supplier (d03), who has an interest in blaming the
installer rather than its own product. d10 then establishes that the d02 entry was a
transcription error (the replacement was on WTG-07, not WTG-14): there was **no** recent
installation. H2 loses its basis, and the lubrication evidence — which was there all along —
makes **H1** the conclusion. A system that handles retraction correctly flips H2→H1 when d10
lands; one that does not stays stuck on H2.

## The planted inventory (why each item is here)

Each item is a `[[planted]]` row in `manifest.toml` with a stable id, a `kind`, and the
verbatim anchor quote(s). The relational ones cross-reference each other (`pair`, `refutes`,
`supports`, `retracts`).

- **Contradiction #1** (`contradiction-1-d01` ↔ `contradiction-1-d02`): d01 says the bearing
  was original and never replaced; d02's log says it was replaced in Nov 2023. Unresolved
  until d10.
- **The overturning fact** (`overturning-fact-d10`, retracts `contradiction-1-d02`): the d02
  entry was for WTG-07. This is the later **overturning fact** the gate is built around.
- **Dissimilar refuter #1** (`dissimilar-refuter-overload`, refutes **H4**): a routine duty
  reading (max 96% of nameplate torque, no excursions) that excludes overload **without using
  the words load / overload / rating** — the §5.1 case a similarity-only candidate funnel
  would miss.
- **Dissimilar refuter #2** (`dissimilar-refuter-counterfeit`, refutes **H3**): the metallurgy
  establishes the steel is correct and in-spec, excluding a counterfeit/defective part
  **without any counterfeit/genuine/non-OEM vocabulary**.
- **Self-serving judgement** (`d03-self-serving-judgement`, §9.1): the supplier blames the
  installer (supports H2) and clears its own product — discount for interest.
- **Genuine observation** (`d03-genuine-observation`): a credible primary measurement in the
  same document — the observation-vs-judgement split.
- **Admission against interest** (`d06-admission-against-interest`, supports **H1**): the
  site team admits an overdue oil change — credibility-boosted, and it points at the true
  cause.
- **Coreference** (`d06-entity-coref`): "the HSS bearing" / "bearing 3" / "it" are **one**
  entity (the §5.2 under-merge case).
- **Over-merge trap** (`d06-over-merge-trap`): "bearing 4" (non-drive-end) is a **different**
  entity on the same shaft — merging it with bearing 3 manufactures a false contradiction.
  Its ground-truth distinction is the d04 hierarchy.
- **Component hierarchy** (`d04-component-hierarchy`): the reference-tier part-whole structure
  and the bearing-3 vs bearing-4 authority.
- **Load-bearing tail fact** (`load-bearing-tail-fact`, supports **H1**): a substitute
  under-viscosity oil grade (VG 150 vs the spec VG 320) buried in the final 10% of the >8,192-
  token purchasing records — the G1.13 tail-window-coverage test.
- **Hard negation** (`d01-hard-negation`) and **hedge** (`d01-hedge`): negation-preservation
  and modality cases for §3.1 faithfulness; the negation is a *subtle* one (no alarm ≠ no
  lubrication problem).
- **Attribution** (`d06-attribution`): named-source reported speech, not document-voice.
- **Reference hypothesis set** (`d09-reference-hypothesis-set`, §11.2): the four dominant
  failure modes H1–H4 seed from.

## Contamination rule

Anyone who is going to be *measured* against this corpus — the V2 annotator, the V6 expert,
the E1 baseline operators — must **not** read this README or `manifest.toml` first; they are
the answer key. They work from the documents under `documents/` only. The jargon-free
labelling instructions (Trial V2, `labels/INSTRUCTIONS.md`) are safe to read.

## Scope (V1 vs later)

V1 is **documents + the planted inventory + the hypothesis set only**. There are **no gold
labels** here — `gold_edges` / `gold_hypothesis_states` / `gold_faithfulness` /
`gold_entity_clusters` / `gold_levels` are Trial V2 and will live under `labels/`. No
extraction or ingest is run in V1; the automated tests are model-free self-consistency checks
on the inventory.

## Extending the corpus

Add a document under `documents/`, register it in `manifest.toml` (`[[documents]]` with a
`role`), add any `[[planted]]` rows (stable id + `kind` from the closed `PlantedKind`
vocabulary + verbatim quote(s)), and the loader + `tests/unit/test_gate_corpus.py` pick it
up. Keep documents one-paragraph-per-line so anchor quotes stay single-line substrings, and
keep d08 above its multi-window word floor.
