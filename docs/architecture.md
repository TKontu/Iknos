# Knowledge Extraction & Reasoning Graph — Architecture

Turn arbitrary text inputs into a queryable reasoning graph: extract facts, link
them into conclusions and hypotheses, keep source references throughout, so the
system can support analysis of open-ended problems.

## Design principles

1. **No single optimal chunk size.** Granularity is multi-level and chosen by
   downstream use, not by searching for one ideal window.
2. **Embed once, index many.** Contextualize the whole document a single time;
   derive every granularity from the cached result. Levels are stored as offset
   ranges, not as copies. Text is stored once.
3. **Retrieval and extraction want different granularities.** Retrieval wants
   self-contained, query-sized passages; extraction wants enough surrounding
   context to resolve references. Neither size is forced to serve both.
4. **Traceability is mandatory.** Every node and edge carries a reference back to
   the source span it came from. Conclusions and hypotheses stay walkable back to
   their underlying facts.
5. **The chunk-quality metric is a tunable knob, not the goal.** What matters is
   that information is extracted, indexed, recallable, and that links are
   identified. The scoring function is an implementation detail.
6. **Symbolic state is authoritative; the LLM proposes, the engine disposes.** The
   LLM never mutates a maintained conclusion directly. Every LLM output is a
   defeasible, provenance-stamped input edge; consistency, acceptance, and retraction
   are decided by the symbolic layer.
7. **Build, not buy; self-hosted and open-source.** No commercial components. The
   first deployment is self-hosted. Where a fork is needed, prefer permissive
   licenses and treat copyleft (GPL) dependencies as reference implementations to be
   re-implemented, not shipped.
8. **Present the network, not a verdict.** The system surfaces ranked probable
   causes, each with its evidence subgraph and reasoning, in traceable form. It is
   not required to converge to a single answer; unresolved, balanced, or circular
   evidence is a finding to surface, not a defect to smooth away.
9. **Auditability is a first-class constraint.** Every conclusion is walkable back to
   the source spans and the operator decisions that produced it. Provenance (§10),
   bitemporal non-lossy history (§7.4), and the propose/dispose split (principle 6)
   exist to serve this; no step may produce a node or edge that cannot be explained.

## Pipeline

```
source text
  → 1. embedding substrate      (contextualize once, cache)
  → 2. segmentation backbone     (multi-level boundaries)
  → 3. proposition layer         (atomic fact units)
  → 4. indexing                  (dense + sparse + graph nodes)
  → 5. reasoning graph           (typed nodes, evidential edges)
```

Each stage references offsets produced upstream. Nothing is re-stored.

## 1. Embedding substrate

- Run a long-context embedding model over the whole document once. Cache the
  contextualized token embeddings.
- Boundary detection, multi-level pooling, and semantic search all run over these
  cached vectors. Re-embedding each level separately would cost roughly L× and
  discard cross-unit context; this avoids both. (Technique: *late chunking*.)

## 2. Segmentation backbone

Produces contiguous, non-overlapping, variable-length segments placed at semantic
boundaries, available at several levels.

- **Boundary signal.** Dissimilarity between adjacent windows as the window slides.
  Topic boundaries are local minima of adjacent-window similarity (valleys). The
  signal is not a single chunk's embedding having an extremum — it is the
  transition *between* windows.
- **Reliable detection.** Smooth the similarity sequence, score each valley by its
  depth (how far it sits below neighboring peaks), and threshold adaptively
  (mean − k·σ). Do not take raw argmin.
- **Placement.** Dynamic programming over sentence boundaries: choose the
  segmentation that maximizes intra-segment coherence minus a length penalty. This
  gives optimal variable-length segments in one pass over sentence units — no
  O(n²) brute-force search over position × size, no overlapping windows to
  deduplicate. (Technique: C99 / DP-segmentation family.)
- **Level knob.** The length penalty. Light penalty → many small segments
  (sub-paragraph grain); heavy penalty → few large ones (chapter grain). One
  mechanism, run at a few penalty settings, produces the whole ladder. Segments
  are stored as offset ranges.
- **Objective caution.** "Good segment" has three competing meanings that pull
  different ways:
  - *coherence* (low internal variance) — a trap: it rewards redundancy, since
    saying one thing five ways scores high while carrying little;
  - *information content* (entity/number density, token surprisal) — what is
    actually wanted;
  - *self-containment* (no references leaking across the cut) — retrieval quality.
  The DP objective should blend coherence with an information signal so segments
  do not collapse onto repetitive blobs. The blend is a knob, not a research goal.
- **Higher levels are summaries, not just longer windows.** A longer window's
  pooled embedding is diffuse; a summarized parent stays sharp. Build coarse levels
  by clustering and summarizing finer ones into a tree. (Technique: RAPTOR-style.)

## 3. Proposition layer — the value layer

- The sub-paragraph grain is the primary unit of value, but only once transformed
  into **propositions**: atomic, self-contained factual statements.
- **Propositionizing = decontextualize.** Resolve pronouns to their referents,
  attach qualifiers, split compound claims.
  Example: "He argued it was insufficient" → "Smith argued the 2019 flood-defense
  budget was insufficient."
- **Why.** Atomic propositions retrieve better than raw sentences (which break on
  unresolved references) and better than passages (too diffuse).
  (Technique: *Dense X Retrieval*, Chen et al., 2023.)
- This step is also the front half of fact extraction: the same transformation
  that makes a span retrievable makes it graph-ready.

## 4. Indexing

Three indexes over the same shared offsets — no duplicated text.

- **Dense** — the cached embeddings, for semantic similarity.
- **Sparse lexical** — a TF-IDF / BM25 term index. Catches exact tokens that
  embeddings blur: names, codes, acronyms, rare jargon. Word-frequency keywording
  belongs here, **not** in the graph.
- **Graph nodes** — see §5.

Retrieval is **hybrid** (dense + sparse): semantic recall plus exact-match
precision.

**Keyword note.** Word-frequency keyworders (raw counts, RAKE, YAKE, TextRank,
KeyBERT) serve the lexical index, not the graph — they emit strings, not typed
entities. Raw counts are the weak floor: they shred multi-word terms and favor
generic-frequent over rare-distinctive. If a stronger sparse signal is wanted,
TextRank (PageRank over a word co-occurrence graph) or KeyBERT (phrases nearest
the document embedding, with a diversity penalty) are cheap upgrades.

## 5. Reasoning graph

**Nodes (typed):**

- **Facts** — each carrying its actors and objects.
- **Deductive conclusions** — derived by logical necessity from facts or other
  conclusions.
- **Inductive conclusions** — generalized from facts; provisional, overturnable by
  new facts.
- **Hypotheses** — marked supported / unsupported / refuted by the facts and
  conclusions linked to them.

Nodes come from the extraction pass (the actors and objects of propositions),
typed and deduplicated — never from word counts.

**Edges are evidential, not co-occurrence:**

- `derived-from` — conclusion ← the facts/conclusions it rests on
- `supports` / `refutes` — fact or conclusion → hypothesis
- relational edges between actors and objects (subject–relation–object) as needed

An evidential edge to a hypothesis carries **two distinct quantities**, deliberately
not collapsed into one number (the §10 schema details them as `sign`, `strength`,
`significance`):

- **significance** — how much the evidence matters *if true*. Largely a property of
  the evidence node and its source/tier (§9): an authoritative measurement weighs
  more than an offhand remark. Relatively stable across hypotheses.
- **connection strength** — how strongly *this* evidence bears on *this* hypothesis,
  with a direction (**sign**: supports vs refutes). A property of the edge: the same
  fact can be decisive for one hypothesis and irrelevant to another.

They are separated because they fail differently and are checked differently:
significance is mostly inherited from metadata and barely depends on LLM judgment,
while connection strength is the genuinely hard judgment to get right (§8).

Co-occurrence ("appeared near each other") is too weak to carry this. Edges come
from relation extraction over the proposition units.

**Edge quality is the determinant.** Nodes are the easy part. Whether the
knowledge-network analysis works at all is decided by whether the evidential edges
are correctly typed and traceable to source facts.

### 5.1 Candidate generation — which pairs to assess

Edge adjudication (the §8 sign+strength LLM judgment) is expensive, so it must run
only on pairs worth assessing. All-pairs assessment is O(n²) LLM calls — intractable
at any real scale, and most pairs are unrelated. **Candidate generation (cheap,
high-recall, approximate) and edge adjudication (expensive, high-precision, LLM) are
two separate stages**; the doc elsewhere describes the second, this describes the
first. It is the standard *blocking / candidate-generation* pattern from entity
resolution and link prediction: a funnel that spends compute in inverse proportion to
how many pairs survive.

The funnel, cheap to expensive:

1. **Structural priors** — near-free. Two propositions sharing an `Actor`/`Object`
   (via `INVOLVES`), or co-occurring in the sparse/keyword index, are candidates;
   restrict to the active box/tier scope. Filters the bulk.
2. **Embedding nearest-neighbour** — for each node, its k-NN in pgvector are
   relatedness candidates. Sublinear (approximate NN), not all-pairs; reuses the dense
   index that already exists for retrieval. The workhorse stage.
3. **Coarse-to-fine (abstract → precise)** — reuse the §2 multi-level chunk hierarchy
   as a pruning tree: match at the coarse level (which sections/paragraphs relate at
   all), then descend to proposition-level pairing only *within* surviving coarse
   matches. The abstraction levels are not only for retrieval; they prune the
   candidate space.
4. **LLM adjudication** — the §8 sign+strength judgment runs only on survivors.

**Tune for recall early, precision late.** A missed candidate is an edge never
considered — a silent false negative, the dangerous kind; a spurious candidate is
just cheaply rejected at adjudication. So the cheap stages favour recall; precision is
the LLM stage's job.

**The dissimilar-refuter problem.** Candidate generation must not rely on semantic
similarity alone. A *refuting* fact can be semantically dissimilar to the hypothesis
it attacks, so embedding-NN under-generates refutation candidates and would bias the
system toward finding support and missing contradiction — a serious flaw given how
central refutation is to the design. Mitigation: a hypothesis must also pull
candidates by its *constituent entities and topic* (structural stage), not only by its
embedding, and contradiction search (the `find-contradiction` operator, §6) is run as
a first-class generator, not a by-product of similarity.

This stage sits between *retrieve* and *expand* in the investigation loop (§11).

## 6. Realization — storage, orchestration, interface

Borrowed pattern: the **enricher-orchestrator** skeleton from flowsint (an OSINT
graph tool — select a node, run an async operator, stream new nodes/edges into the
graph). The skeleton is reused; flowsint's OSINT operator catalog (DNS, WHOIS,
breach lookups) and its monotonic assumptions are discarded.

**Operators** replace flowsint's enrichers. Each takes a node and expands the
graph, runs async (slow LLM/retrieval work), and streams results to the UI:

- `extract` — span → facts (actors, objects)
- `deduce` — facts/conclusions → deductive conclusion
- `induce` — facts → inductive conclusion (provisional)
- `corroborate` — hypothesis → supporting / refuting facts and conclusions
- `find-contradiction` — surface conflicting facts/conclusions. A single LLM call
  detects contradictions barely above chance, so this operator requires agreement
  across multi-sample LLM judgment, a symbolic consistency check, and (where time
  matters) a temporal check before it may assert a `refutes` edge.

**Storage** — a single self-hosted, open-source engine: **PostgreSQL + Apache AGE +
pgvector**.

- **Apache AGE** (Postgres extension, Apache-2.0) holds the reasoning graph as a
  single property graph: typed vertices, evidential edges. One graph, not one per
  box — cross-box edges and entity deduplication require it (see §9).
- **PostgreSQL** (same instance) holds source text, span offsets, auth, and
  metadata as relational tables; `pgvector` holds the dense embedding index.
  Provenance resolution (Span → text) is therefore a local join, not a cross-engine
  hop, and belief-revision's multi-row updates ride Postgres MVCC.
- **Network analysis** is not done in the database. Extract the (small)
  per-investigation working subgraph and run **igraph** (or NetworkX): centrality
  (betweenness / PageRank) for load-bearing facts, community detection
  (Louvain / Leiden) for sub-arguments, weighted pathfinding for hypothesis support.
  This assumes working sets fit in memory — true at investigation scale; if a single
  graph ever reached millions of nodes, in-database analytics would have to be
  reconsidered. Transitive reachability for retraction uses Postgres `WITH RECURSIVE`
  rather than a graph traversal.

**Module split** (mirrors flowsint): types / core-orchestrator / operators / api
(with real-time event streaming) / app (node-expansion canvas, kept performant on
large graphs). Orchestration uses an open-source task queue; the LLM is a
swappable component (hosted API for the MVP, open-weight self-hosted model when
full self-hosting is required).

## 7. Non-monotonic layer — what flowsint does not have

flowsint's enrichers are deterministic and its graph only grows: a DNS record is a
verified fact that never has to be retracted. Reasoning operators are fallible, and
a newly extracted fact can overturn an earlier conclusion. Four additions follow:

1. **Edge confidence.** `supports` / `refutes` / `derived-from` carry a strength,
   not a boolean.
2. **Hypothesis state machine.** Each hypothesis is recomputed — supported /
   unsupported / refuted — from its current incoming evidence and the confidences
   on those edges. A flip to `refuted` requires the ensemble gate (multi-sample LLM
   + symbolic + temporal agreement), never a single judgment.
3. **Belief revision.** When a new fact lands, every conclusion and hypothesis
   downstream of it is re-evaluated and may be downgraded or retracted. The graph
   is non-monotonic: nodes can lose support, not only gain it.
4. **Bitemporal record.** Never hard-delete. Each fact and edge carries event time
   and ingestion time with a validity window; superseded facts are invalidated, not
   removed, preserving "what did we believe at time T." This is the Graphiti/Zep
   pattern; we implement it directly on our own edges (open-source, schema stays
   ours) rather than adopting it as a dependency — it handles validity windows, not
   adjudication, which layers on top.

Provenance stays heavier than flowsint's: every node and edge traces back to a
proposition and a source span, not merely to another node.

## 8. Belief-revision design space

Belief revision is four separate sub-problems, not one. The error would be to use a
single framework for all of them.

**a. Dependency tracking** — if a node's support changes, find and update what
depends on it. Our `derived-from` edges are already this structure. Truth
Maintenance Systems (JTMS / ATMS) are the conceptual ancestor; the implementation
is **incremental view maintenance** with the **Counting** discipline (a conclusion
survives retraction of one support while other supports remain). Open-source path:
Differential Dataflow / DBSP (Feldera) for symmetric add/delete on recursive
queries, or a Counting-based propagation implemented directly over our graph for
the MVP. Provenance semirings (annotate each conclusion with a polynomial over its
source facts) are the principled model and instantiate to a confidence algebra.
(Commercial engines like RDFox are excluded by the build/open-source principle.)

**b. Conflict adjudication** — when supports and refutes coexist, decide the
verdict. This is computational argumentation. Because our edges are graded, the fit
is a **Quantitative Bipolar Argumentation Framework (QBAF)** with a gradual
semantics (DF-QuAD or Quadratic Energy), which yields real-valued acceptability
from intrinsic weights plus attack/support — not boolean Dung extensions. For
acyclic support/attack this recomputes locally and cheaply. Boolean acceptance
(SAT-based incremental solvers) is reserved for the narrow case of formal
credulous/skeptical acceptance, if ever needed. The gradual semantics are small
algorithms we implement in-house (QBAF-Py / Uncertainpy as reference only).

**c. Confidence scoring** — combine graded evidence of varying reliability.
Options by rigor: weighted aggregation / certainty factors (pragmatic); Dempster-
Shafer (misbehaves under high conflict — Zadeh counterexample); subjective logic
(models belief / disbelief / ignorance plus source-trust fusion — best fit);
Bayesian networks (need a DAG and CPTs we cannot populate from LLM output).

**d. Recomputation efficiency** — dirty-marking + lazy recompute-on-read, bounded
propagation depth, or the incremental-Datalog machinery from (a).

### Decisions

- **Adjudication: gradual, not boolean.** Model supports/refutes as a QBAF; compute
  hypothesis state from gradual strengths (DF-QuAD or Quadratic Energy). Boolean SAT
  acceptance only if formal credulous/skeptical acceptance is ever required.
- **Confidence pipeline.** Do not feed raw verbalized LLM confidence as edge weight.
  (1) Elicit by multi-sample consistency, not single-shot verbalization; (2)
  recalibrate per model; (3) encode each judgment as a subjective-logic opinion with
  source-reliability discounting; (4) fuse correlated/conflicting evidence with
  cumulative or averaging fusion — never raw Dempster's rule under conflict.
- **Edge-judgment disciplines (LLMs are poor, biased edge judges).** The connection
  `strength` is the hardest value to get right, so the protocol is hardened against
  known LLM failure modes:
  - **Sign before magnitude.** Classify direction (supports / refutes / irrelevant)
    *first and separately*, then estimate strength only for non-irrelevant edges. A
    wrong sign is catastrophic; a noisy magnitude is absorbed by the gradual semantics.
  - **Relative, not absolute.** Elicit strength by ranking / pairwise comparison of
    competing evidence on the same hypothesis, not abstract 0–1 scores; ordering is
    more stable than absolute numbers, and the gradual semantics depends mostly on
    ordering.
  - **Blind and randomized judging.** Judge each edge *blind to the current
    hypothesis state* (a sycophancy guard against rubber-stamping the leading
    hypothesis) and present evidence in randomized order across samples (cancels
    position bias). Protocol disciplines, not new research.
  - The LLM produces a sign and a *raw* strength; the stored edge `strength` is the
    fused, recalibrated, expert-correctable result (§10, §13).
- **Dependency/retraction: two layers (§12).** Truth maintenance over a commutative
  group (integer derivation-support counts; Counting/DRed for the MVP, Differential
  Dataflow / DBSP for scale) is kept separate from confidence valuation over an
  absorptive semiring (Viterbi `max-·`, or Gödel `max-min`). The split is forced by
  algebra: deletion needs an additive inverse, confidence needs idempotence, and one
  structure cannot have both.
- **Source trust = entrenchment.** AGM says drop the least entrenched belief first;
  entrenchment maps to source reliability and drives both edge confidence and
  revision priority.
- **Layering**, all on the AGE property graph: IVM/Counting structure → QBAF gradual
  adjudication → subjective-logic scoring → LLM local judgments written back with
  provenance and a bitemporal state-transition log. Network analysis runs on
  extracted subgraphs, not in the database.

### Staged build

0. **Invariant.** Symbolic state authoritative; LLM proposes, engine disposes
   (design principle 6).
1. **Retraction core (Layer A).** Group-valued derivation counts via Counting over
   the graph; a conclusion survives iff its support count stays positive (§12).
2. **Confidence valuation (Layer B).** Viterbi (or Gödel) least-fixpoint over the
   supported sub-graph; recompute only the delta-affected region (§12).
3. **Gradual adjudication.** In-house QBAF + DF-QuAD / Quadratic Energy for
   hypothesis state, consuming Layer B confidence as base scores.
4. **Confidence fusion.** The four-step pipeline above (feeds Layer B inputs).
5. **Bitemporality.** Event + ingestion time, validity windows, non-lossy
   supersession (§7.4).
6. **Ensemble contradiction.** Multi-sample LLM + symbolic + temporal agreement
   before any `refutes`.

### Custom work (no off-the-shelf solution — the novel part)

No published system combines all three; this is where original engineering is
unavoidable:

- **Weighted-truth propagation** under retraction — resolved as the two-layer model
  (§12: group-valued counts + absorptive-semiring confidence). The remaining
  engineering is integrating the two layers cleanly; no off-the-shelf system packages
  this combination.
- The **mapping from calibrated LLM judgments to QBAF intrinsic weights** and
  attack/support edges.
- **Incremental maintenance of gradual hypothesis state** over the evolving
  bitemporal graph.

### Tooling

- **Production-ready, used directly:** PostgreSQL + Apache AGE + pgvector (single
  engine), igraph / NetworkX for network analysis over extracted subgraphs, clingo
  (ASP, for symbolic consistency checks and defeasible rules), Differential
  Dataflow / DBSP.
- **Reference only, re-implemented in-house:** QBAF gradual semantics (QBAF-Py is
  GPL-2.0; Uncertainpy), subjective-logic operators, LLM-confidence calibration.
- **Borrowed as a pattern, not a dependency:** Graphiti's bitemporal edge model.

### Proposed small-scale experiment

Build the MVP layer (stages 1–2) on a small fixed corpus with deliberately planted
contradictions and a later source that overturns an earlier claim. Seed facts →
derive a handful of conclusions and 2–3 hypotheses → inject the overturning fact.
Measure: (a) does Counting-based retraction propagate correctly and stay local; (b)
does the QBAF hypothesis state flip correctly when the overturning fact lands; (c)
how much better is consistency-based confidence than raw verbalized confidence on
the planted set; (d) does ensemble contradiction detection beat a single LLM call;
(e) does candidate generation (§5.1) recall the planted edges — especially the
*refuting* ones, which test the dissimilar-refuter problem — before adjudication.
Goal: learn the real cascade behavior, the confidence-calibration gap, and the
candidate-recall gap before hardening any layer.

## 9. Knowledge tiers and boxes

Two orthogonal axes.

**Tier — epistemic role; drives reasoning and entrenchment.** A small fixed set:
schema/ontology → reference/authority → case evidence → working conclusions.
Entrenchment decreases and volatility increases down the stack. Tiers are *soft*:
boundaries are revisable and nothing is un-retractable — the term "ground truth" is
avoided deliberately, because reference knowledge is still defeasible and in
root-cause work the valuable signal is often the case that contradicts it.

**Box — lifecycle/provenance unit; drives management.** An open, growing set. Each
box sits in exactly one tier, but many boxes can share a tier (a failure-mode
encyclopedia and a materials textbook are both reference-tier, different boxes). A
box carries: tier, version, source, a reliability/entrenchment prior, a validity
window, and status (active / deprecated). This is the TBox/ABox idea generalized:
reference boxes are mostly TBox (rules, taxonomies, the candidate-cause space), case
boxes are ABox (specific observations).

**Source vs working.**

- **Source boxes** (append-on-ingest, versioned, stable): reference boxes and
  case-evidence boxes.
- **Working box** (mutable, disposable, one per investigation): the conclusions,
  hypotheses, and `supports` / `refutes` / `derived-from` edges the operators create.
- An investigation = selected reference boxes + case-evidence box(es) + one working
  box. Reasoning reads across all active boxes and writes only to the working box
  (except gated promotion). Drop the working box and the investigation is gone with
  sources intact; reference boxes are reused across investigations.

**Cross-box edges belong to the working box**, not to either endpoint's box. When a
case fact `refutes` a reference principle, that edge is part of the investigation's
reasoning, so source boxes are never mutated by reasoning about them.

**Soft separation, separate management.** One AGE graph in one Postgres instance.
Boxes are *not* separate AGE graphs — cross-box edges and entity deduplication
(the cross-box-edge rule above) require a single graph. They are a logical
partition instead: a `box` property/label on every vertex and edge plus a `(:Box)`
registry vertex holding the metadata above. Management operations are scoped by
`box`; reasoning is scoped by tier and reliability. The dense / sparse / graph
indexes carry the box id so retrieval can be limited to the active working set.
Co-locating the graph with the relational and vector data in one engine makes
box-scoped management plain SQL.

**Connections to the non-monotonic layer.** Deprecating a box (e.g., a retracted
paper) flips its status and triggers belief revision on everything `derived-from`
its facts. **Promotion** — a validated working conclusion earning its way into the
reference tier — is a deliberate, gated change of box membership, never automatic;
automatic promotion would let tentative case conclusions contaminate the shared base.

## 10. Schema (the contract)

Stated once here so it is not reconstructed from prose elsewhere. This is the
authoritative data model; other sections defer to it. Nodes and edges below are AGE
vertices and edges; raw text, offsets, and the vector index are ordinary relational
tables in the same Postgres instance, joined to the graph by id.

### Node labels

- **`Document`** — an ingested source. Properties: `id`, `box` (id of the box it
  belongs to), `uri`/`title`, `ingested_at`. Raw text and offsets live in
  PostgreSQL, keyed by `Document.id`; the graph holds the node, not the text.
- **`Span`** — a contiguous source range, the unit of provenance. Properties: `id`,
  `document_id`, `start` and `end` (character offsets into that document's text),
  `level` (segmentation level that produced it). A Span is the *only* thing that
  points at raw text; every provenance reference resolves to one or more Span ids.
- **`Proposition`** — a decontextualized atomic statement (the §3 unit). Properties:
  `id`, `text` (the rewritten, self-contained form), `box`. Linked to the Span(s) it
  came from. Propositions are first-class nodes, not free text on other nodes.
- **`Actor`**, **`Object`** — entities. **These are nodes, not properties of a
  fact.** Properties: `id`, `label`, `type`, `box`. Deduplicated across the box set
  of an investigation.
- **`Fact`** — an asserted state of affairs extracted from a Proposition. Properties:
  `id`, `box`, `tier`, plus the bitemporal and confidence fields below. A Fact links
  to its Actors/Objects (by `INVOLVES`) and to its Proposition and Span(s) (by
  `EVIDENCED_BY`).
- **`DeductiveConclusion`**, **`InductiveConclusion`** — derived claims. Same
  property set as Fact. Inductive is provisional by definition.
- **`Hypothesis`** — a claim under test. Properties: those of Fact, plus
  `state` ∈ {supported, unsupported, refuted} (computed, never set by hand) and
  `acceptability` (the real-valued QBAF strength it was computed from).
- **`Box`** — the registry node (§9). Properties: `id`, `tier`
  ∈ {schema, reference, case, working}, `version`, `source`, `reliability_prior`,
  `valid_from`, `valid_to`, `status` ∈ {active, deprecated}.

Every node except `Box` and `Document` carries `box` (its lifecycle unit) and, where
it participates in reasoning, `tier`. `tier` on a node is inherited from its `Box`
unless explicitly overridden (e.g., a promoted node).

### Edge types

- **`EVIDENCED_BY`** — claim → Proposition / Span. The provenance link; every Fact,
  Conclusion, and Hypothesis has at least one.
- **`INVOLVES`** — Fact/Conclusion/Hypothesis → Actor / Object, with a `role`
  property (subject, object, instrument, …).
- **`DERIVED_FROM`** — Conclusion → the Fact(s)/Conclusion(s) it rests on.
- **`SUPPORTS`**, **`REFUTES`** — Fact/Conclusion → Hypothesis (or Conclusion). The
  edge type is the `sign`; it carries `strength` and an evidence `significance` (see
  properties below).
- **`RELATES`** — Actor/Object → Actor/Object, with a `relation` property
  (subject–relation–object).

### Properties carried by reasoning nodes and evidential edges

- **Evidential-edge values** (`SUPPORTS` / `REFUTES`) — two distinct quantities,
  never collapsed into one number:
  - **`sign`** — direction, carried by the edge type itself (`SUPPORTS` vs
    `REFUTES`). Determined first and separately (§8), because a wrong sign is
    catastrophic while a slightly-off magnitude is absorbed by the gradual semantics.
  - **`strength`** — connection/relevance in [0, 1]: how strongly this evidence bears
    on this hypothesis. The hard, calibrated number. The LLM produces a *raw* strength
    (a noisy judgment); the stored `strength` is the **fused, recalibrated,
    expert-correctable** value (§8 pipeline), not the raw LLM output — principle 6 at
    the edge level: the LLM proposes the judgment, the scoring layer disposes the
    number.
  - **`significance`** — weight of the evidence if true, in [0, 1]: largely inherited
    from the evidence node's source/tier (§9), so it barely depends on LLM judgment.
- **`confidence`** (edge `DERIVED_FROM`, and Layer-B node confidence) — strength in
  [0, 1], a calibrated value (§8); the propagated, double-counting-free value from
  §12, **not** a raw LLM number.
- **Bitemporal** (Facts, Conclusions, Hypotheses, and evidential edges) —
  `event_time` (when the asserted thing held in the world), `ingested_at` (when the
  system learned it), `valid_from` / `valid_to` (the validity window; `valid_to` null
  = currently held). Supersession sets `valid_to`; nothing is deleted.
- **`provenance`** — resolved transitively: a claim's provenance is the set of Span
  ids reachable via `EVIDENCED_BY` and, for conclusions, via `DERIVED_FROM` to the
  underlying facts' Spans. A conclusion is traceable to source iff this set is
  non-empty.
- **`override`** (optional, any reasoning node or edge) — a soft expert override of a
  computed attribute (§10.3). Null on machine-produced values. When present it holds
  the overriding value, the prior computed value, the actor, timestamp, and
  rationale; the computed value underneath is never overwritten.

### Resolution rule

A provenance reference is always a `Span` id (or a set of them), never an offset
embedded on another node. To get text: `Span → (document_id, start, end) →`
PostgreSQL lookup — a local join in the same engine, since the graph (AGE) and the
text tables share one Postgres instance. This keeps text stored once and offsets in
one place.

### 10.1 Process action log

Auditability has two complementary forms. The first is a **chronological log of
process actions** — the record of what the system *did*, separate from what it
*holds*. Every operator run and every belief-revision event appends an immutable
`Action` record (a relational table, not graph nodes), with: `id`, `timestamp`,
`actor` (operator name, or user id for manual actions), `action_type`
(extract / deduce / induce / corroborate / find-contradiction / supersede / override
/ promote), `inputs` (the node/edge/span ids consulted), `outputs` (the node/edge ids
created or changed), and, for LLM-backed actions, `model`, `sampling` (the
multi-sample regime), the raw judgment, and the calibration applied. The log is
append-only; it is the process narrative and the basis for replay.

### 10.2 Per-node and per-edge auditability

The second form is **point auditability**: from any node or edge in the graph view,
an expert can see everything that explains it — its attributes, its provenance
(`EVIDENCED_BY` → Spans → source text), the `Action` record(s) that produced or last
changed it (joined by output id), its bitemporal history (every prior
`valid_from`/`valid_to` interval, since supersession is non-lossy), and any
`override`. No node or edge may exist that cannot answer "where did you come from."

### 10.3 Soft override (expert-in-the-loop)

This is an expert tool: analysis results are an **initial conclusion**, a starting
point for expert review, not a final answer (principle 8). An expert may override
any computed attribute, content, or edge from the graph analysis view — and under
principle 6 the human is the one actor permitted to *dispose*, so an override is the
sanctioned way to change a maintained value. It must be **soft**, never destructive:

- The computed value is **retained**; the override is a layer on top (the `override`
  property), carrying overriding value, prior computed value, actor, timestamp, and
  rationale.
- Override is **per-property**, not per-object. On an evidential edge the expert may
  override `strength` (the common case — recalibrating how strongly evidence bears on
  a hypothesis) or `sign` (the rarer, stronger act of reversing supports↔refutes);
  each is overridden and tracked independently.
- An override is **logged** as an `Action` (§10.1) and is itself bitemporal and
  reversible — reverting restores the computed value because it was never lost.
- Overrides **participate in reasoning** like any disposed value: an overridden
  strength, sign, or confidence feeds Layer A/B and the QBAF, and propagates
  downstream. But they are **marked**, so the system (and any reviewer) can always
  distinguish expert-set values from machine-derived ones, and can recompute the
  machine-only view by ignoring the override layer.
- Override divergence is a **calibration signal**: the gap between an expert-set value
  and the machine value it replaced is logged and fed back as a per-operator /
  per-model bias measurement (§8, §13). Expert correction is how the system's edge
  judgments improve over time, not just a one-off fix.

**Reconciliation when re-derivation changes the value beneath an override.** A later
re-derivation may move the computed value under an active override. The response
depends on *direction* and on whether *new evidence entered the basis* — not merely
on the number moving:

- **Default — hold with a divergence flag.** The expert's value stays in force; the
  machine value updates beneath it; the divergence is shown. Never silently discard
  the expert's judgment (that would invert principle 6), and never silently let it go
  stale (the flag makes drift visible).
- **Escalate to prompt** when *new evidence entered the basis* of the re-derivation
  **and** the change is material (past a threshold) or crosses a state boundary
  (supported ↔ unsupported ↔ refuted). The expert's judgment was made without that
  evidence, so the one permitted disposer is asked to reconcile. A pure recompute over
  the *same* evidence the expert already saw does **not** prompt — they already
  adjudicated it.
- **Auto-release on convergence** — distinct from auto-revert — when the machine
  settles within ε of the override: the override is now redundant, so its release is
  *suggested* (not silently applied), simplifying the graph without losing anything.
- **Never auto-revert on divergence.** The machine must not overrule the human by
  dropping a contested override; divergence is exactly the case that escalates to
  prompt or holds with a flag.

The discriminator is whether new information entered the basis, because an override is
the expert's judgment *given the evidence they saw*: unchanged evidence → judgment
stands; grown evidence → judgment may be stale and the expert is asked.

The invariant: at any time the system can show both what it computed and what the
expert asserted, and explain every divergence. Manual knowledge never erases machine
knowledge, and vice versa.

## 11. Investigation loop (runtime)

The §1–5 pipeline is how knowledge gets *in*. This is how an investigation *runs*
over it. Ingest and investigation are distinct flows; do not conflate them.

```
assemble working set   select active boxes: case-evidence box(es) + relevant
                       reference boxes; create one working box. Seed each edge's
                       confidence prior from its source box's reliability_prior.
        ↓
retrieve               hybrid (dense + sparse) over the active boxes only, scoped
                       by box id; pull candidate facts/propositions.
        ↓
generate candidates    funnel cheap→expensive (§5.1): structural/keyword priors →
                       embedding k-NN → coarse-to-fine over §2 levels. Recall-biased.
                       Pull refutation candidates by entity/topic, not similarity
                       alone, so contradiction is not structurally missed.
        ↓
expand (operators)     run extract / deduce / induce / corroborate /
                       find-contradiction on selected nodes and the candidate pairs.
                       Writes land in the working box only. find-contradiction and any
                       REFUTES require the ensemble gate (§7.2).
        ↓
adjudicate             recompute affected hypothesis state via QBAF gradual
                       semantics; propagate retraction by Counting (§8). Only the
                       affected sub-graph is touched.
        ↓
analyse                extract the working subgraph into igraph/NetworkX for network
                       analysis: centrality (load-bearing facts), community
                       detection (sub-arguments), hypothesis-support pathfinding.
        ↓
present                a result is a Hypothesis with its state, acceptability score,
                       its SUPPORTS/REFUTES subgraph, and the resolved provenance
                       (Span → source text) for every node in that subgraph.
        ↺
revise                 new evidence — or an expert soft override (§10.3) — re-enters
                       at "expand"; downstream conclusions and hypotheses are
                       re-evaluated, not recomputed wholesale.
```

A result is never a bare answer: it is always the claim plus its evidence subgraph
plus the provenance trail, so a human can audit why the system holds it.

## 12. Propagation model — two layers, not one

The "confidence-aware semiring" open item is resolved as follows: **truth maintenance
and confidence are two separate algebraic objects, not one.** This is forced by
algebra, not preference — clean incremental deletion needs an additive inverse (a
group), and confidence aggregation needs idempotence (no inverse); the two cannot
coexist in one non-trivial structure (`a + a = a` plus an inverse forces `a = 0`).
Any attempt to unify them yields either confidence that double-counts / fails to
converge on cycles, or a "difference" operator that violates the algebraic laws the
confidence calculus needs.

**Layer A — truth maintenance over a commutative group (owns retraction).**
Maintain, per derived node, an integer **derivation-support count**. Insertion
increments, retraction decrements; a node is supported iff its count is positive,
and drops out when it reaches zero. Because the carrier is a group (counts can go
negative as deltas), deletion is exact and incremental. Implementation: the
recursion-capable **Counting** algorithm (or DRed / Backward-Forward) over the AGE
graph for the MVP; **Differential Dataflow / DBSP (Feldera)** fed by Postgres CDC as
the scale path. This layer answers only "is this still supported, and by how many
derivations" — never "how strongly."

**Layer B — confidence valuation over an absorptive, ω-continuous semiring (owns
strength).** On the nodes Layer A certifies as supported, compute confidence as a
least fixpoint over the **Viterbi semiring `([0,1], max, ·, 0, 1)`** — multiply
confidences along a rule body (conjunction), take the max across alternative
derivations (disjunction), i.e. best-derivation confidence. (Use the Gödel /
fuzzy `max-min` semiring instead if the degrees are ordinal rather than
probability-like.) Both are idempotent and absorptive, so the fixpoint is
well-defined and **convergent even on cyclic derivation graphs**, and is
double-counting-free across multiple derivations by construction. Recompute only on
the delta-affected sub-graph Layer A reports as changed. **Never use the
probabilistic sum-product semiring here** unless derivations are provably
independent — it double-counts and can diverge on cycles.

**Why two annotations from day one.** Every fact/edge carries both an integer
support-count (Layer A) and a `[0,1]` confidence (Layer B). Layer A *should* count
multiplicities — that is how it knows when support hits zero. Layer B must *not* —
idempotence is what prevents inflation. Conflating them reintroduces the failure
this split exists to avoid.

**The contract handed to adjudication.** Layer B's `[0,1]` confidence is the clean
strength consumed as a node's intrinsic/base score by the QBAF gradual semantics
(§8). That is the seam between propagation and adjudication: Layer A decides
membership, Layer B scores it, QBAF adjudicates supports/refutes over those scores.

**Escalation paths (only if needed).** If confidence ever needs genuine subtractive
incremental maintenance, escalate to a **valuation algebra with division**
(Kohlas–Wilson) rather than abusing a semiring. If calibrated *probabilities* are
required (not ordinal confidence) and derivations are correlated, move to
probabilistic-database lineage + knowledge compilation and accept the cost
(#P-hard). Neither is in scope for the MVP.

## 13. Risks and unsolved problems

Engineering-flavored research risks carried by §8 and §12; each needs a spike or a
decision before the layer that depends on it is hardened.

- **Cyclic structure is surfaced, not forced to converge.** In Layer B, confidence
  uses an absorptive / ω-continuous semiring (Viterbi or Gödel), which converges even
  on cycles. The downstream QBAF gradual semantics has no general convergence
  guarantee on cyclic argument graphs — but per principle 8 this is no longer a
  blocker: a non-converged or oscillating region is a *finding* (genuinely circular
  or balanced evidence) to be detected, bounded, and surfaced to the investigator,
  not smoothed into a false verdict. Requirement is therefore: bound the iteration,
  detect oscillation, and present the region as unresolved with its subgraph — not
  guarantee a fixed point.
- **Incremental QBAF strength update appears to be an open research gap.** There is
  no published algorithm for incrementally updating QBAF final strengths under graph
  change; known work addresses *explaining* change, not efficiently recomputing it.
  Consequence: incrementality stops at Layer A's delta; the affected QBAF sub-region
  is recomputed in full. Acceptable at investigation scale; revisit if it bottlenecks.
- **Negation / aggregation in rules breaks plain provenance.** Monus-based difference
  provably violates natural axioms, so if rules use negation, restrict to stratified
  negation with the recursion-capable Counting algorithm, or adopt the
  dual-indeterminate provenance approach. Do not assume a simple "subtract" works.
- **The two-layer model is a synthesis, not a packaged result.** No published system
  combines ℤ-count truth-maintenance + Viterbi confidence + QBAF hand-off as one
  named architecture; we are integrating across the database-provenance, IVM, and
  argumentation literatures. The integration seams are ours to validate.
- **LLM→QBAF weight mapping is unstandardized** (also in §8 custom work). Turning a
  calibrated LLM judgment into a base score and an attack/support edge has no
  reference recipe; it must be designed and validated against the planted-corpus
  experiment.
- **LLMs are biased edge judges, and some error is correlated.** Per-edge bias is
  mitigated by §8's disciplines (sign-before-magnitude, relative elicitation, blind /
  randomized judging, multi-sample fusion, recalibration). The residual that those do
  *not* remove is **correlated error**: one model judging many edges shares biases
  across them, so they do not average out and can systematically tilt a whole
  hypothesis. Partial defenses — use genuinely different judges (different
  models/prompts, multi-LLM argumentation), and treat suspiciously uniform edge
  strengths as a red flag — reduce but do not eliminate it. Recorded as a known
  limitation, not a solved problem.
- **Expert overrides are the calibration backstop.** Soft overrides (§10.3) of edge
  values are not only one-off corrections: logged systematically (§10.1), the
  divergence between expert-set and machine-set strengths is a measurable per-operator
  / per-model bias signal that feeds recalibration. The human-in-the-loop closes the
  calibration gap the LLM cannot close alone. (Designing this feedback into the
  recalibration step is open work.)
- **Candidate-generation recall bounds everything downstream.** Edges are only ever
  as complete as the candidate funnel (§5.1) that feeds adjudication: a pair never
  generated is an edge never considered — a silent false negative. The
  **dissimilar-refuter problem** is the sharp case: refuting evidence can be
  semantically dissimilar to its target, so a similarity-only funnel structurally
  under-generates contradiction and biases the system toward support. Mitigation
  (entity/topic-driven candidates + contradiction search as a first-class generator)
  reduces but does not prove away the gap; candidate recall — especially refuter
  recall — must be measured on the planted-corpus experiment, not assumed.
- **Re-evaluation trigger policy is undecided** — eager propagation vs lazy
  recompute-on-read, and the propagation bound. Cheap to defer, but it shapes the
  Layer A↔B interface, so decide before hardening Layer B.
- **Tooling seam: ProvSQL vs DBSP placement.** ProvSQL gives semiring provenance
  *inside* Postgres but is non-recursive and recomputes circuits; DBSP/Feldera does
  clean recursive retraction but runs *alongside* Postgres on CDC. The MVP can avoid
  both with hand-written Counting over AGE, but the scale path requires choosing, and
  the choice affects where Layer A physically lives.

## Open items

- [x] **Storage engine** — resolved: PostgreSQL + Apache AGE + pgvector as a single
  self-hosted engine; network analysis via igraph/NetworkX on extracted subgraphs
  (see §6). Neo4j dropped (GPLv3, second engine, GDS not load-bearing at our scale);
  Kùzu rejected (archived/abandoned Oct 2025).
- [ ] **Cyclic adjudication** — *not* a convergence requirement (principle 8): bound
  iteration, detect oscillation, and surface unresolved/circular regions with their
  subgraph (§13). Open question is the detection-and-presentation policy, not a
  convergence guarantee.
- [x] **Audit log** — resolved; see §10.1 (process action log) and §10.2 (per-node /
  per-edge point auditability).
- [x] **Override re-derivation semantics** — resolved; see §10.3 reconciliation
  policy (default hold-with-flag, escalate-to-prompt on new evidence in the basis,
  auto-release on convergence, never auto-revert; discriminator is whether new
  evidence entered the basis).
- [x] **Confidence-aware semiring** — resolved as the two-layer propagation model
  (§12); group-valued counts for retraction, absorptive semiring for confidence.
- [ ] **Candidate-generation recall tuning** — the funnel thresholds (§5.1),
  especially k for embedding k-NN and the coarse-level cutoff, and how to measure
  refuter recall specifically. Experiment-driven.
- [ ] **Re-evaluation trigger policy** — eager propagation vs lazy recompute-on-read,
  and the propagation bound.
- [x] **Knowledge tiering** — resolved; see §9 (tier × box axes, source vs working,
  gated promotion).
- [ ] **LLM→QBAF mapping** — turning calibrated LLM judgments into intrinsic weights
  and attack/support edges (also listed under §8 Custom work).

## Techniques referenced

- Adjacent-window similarity valleys for boundaries — TextTiling lineage
  (Hearst, 1997); modern "semantic chunking."
- DP segmentation under a coherence/length objective — C99 family.
- Embed-once, pool-later — "late chunking" (Jina, 2024).
- Summarize-upward abstraction tree — RAPTOR (2024).
- Proposition-grain retrieval — Dense X Retrieval (Chen et al., 2023).
- Sparse + dense hybrid retrieval — BM25 with embeddings.
- Cheap keyphrase extraction — TextRank, YAKE, KeyBERT.
- Candidate generation / pair pruning — blocking in entity resolution; approximate
  nearest-neighbour retrieval; link prediction. Coarse-to-fine pruning over the §2
  abstraction levels.
- Enricher-orchestrator pattern and graph storage — flowsint (OSINT graph tool).
- Graph network analysis — igraph / NetworkX over extracted per-investigation
  subgraphs (centrality, community detection, pathfinding).
- Incremental retraction — DRed / Counting incremental view maintenance;
  Differential Dataflow / DBSP (Feldera).
- How-provenance — provenance semirings (Green & Tannen).
- Confidence propagation — Viterbi / Gödel (max-min) semirings; absorptive,
  ω-continuous semirings for convergent fixpoints under recursion (Dannert, Grädel,
  Naaf & Tannen; Khamis, Ngo, Pichler, Suciu & Wang).
- Group-valued incremental retraction — ℤ-relations / DBSP (Budiu, McSherry,
  Ryzhyk, Tannen); Counting / DRed / Backward-Forward (Motik, Nenov, Piro,
  Horrocks).
- Semiring provenance inside Postgres — ProvSQL (Senellart et al.), with monus.
- Valuation algebras (escalation path) — Kohlas & Wilson.
- Gradual adjudication — Quantitative Bipolar Argumentation with DF-QuAD /
  Quadratic Energy (Baroni, Rago, Toni; Potyka).
- LLM-generated arguments → gradual verdict — ArgLLMs (AAAI 2025), MArgE (2025).
- Evidence fusion under uncertainty — subjective logic (Jøsang); avoid raw
  Dempster's rule under conflict.
- Bitemporal LLM knowledge graph — Zep / Graphiti (2025).
- Contradiction detection is near-chance from a single LLM call — Knowledge
  Conflicts survey (EMNLP 2024); hence the ensemble gate.
- Entrenchment by source reliability — AGM belief revision.
