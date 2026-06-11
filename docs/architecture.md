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

**Stage 0 — document parsing (front-end).** Real case documents are PDFs and scans
with multi-column layout, tables, figures, and OCR-only content, not clean text. A
**document parser** front-ends the pipeline, producing the structured input the rest
assumes: reading-order text, document structure (headings/lists/paragraphs), tables as
structured data, located figures with captions, formulas, and **per-element layout
coordinates (page + bounding box)**. This is a *swappable component behind a fixed
contract* (like the LLM): the default implementation is **MinerU** (self-hosted,
open-source, OCR in many languages, layout + table + figure extraction). Three
integration rules: (a) **tables ingest as structured observations** (rows/cells →
propositions with column semantics, §3), not flattened prose — they are observation-class
evidence (§3.1); (b) figures are *located* here and *interpreted* by a vision `extract`
operator downstream (§3), flagged provisional; (c) layout coordinates flow into `Span`
(§10) for **visual provenance** — a claim resolves to a region on the original page.
Parse quality is a faithfulness input: scanned / handwritten / complex-table parses are
marked lower-faithfulness → provisional → triage. **License boundary:** MinerU is
AGPL-3.0, so it is invoked as a **separate hosted service** (CLI/HTTP), never vendored
into the codebase — the copyleft stops at the service edge.

- Run a long-context embedding model over the whole document once. Cache the
  contextualized token embeddings.
- **Documents longer than the model context are windowed, never truncated.** The
  embedding context (e.g. 8k tokens for bge-m3) is far smaller than a real case
  document, so "the whole document once" is implemented as **overlapping
  macro-windows**: embed consecutive max-length windows with a fixed overlap, and
  pool each span from the window where it sits furthest from a window edge (late
  chunking over windows). **Silent truncation is forbidden** — an ingest that cannot
  cover the full document must fail loudly, not index a prefix; the window layout is
  recorded in the segment Action so coverage is provable from provenance.
- **"Cache" is scoped honestly.** Token embeddings are cached per ingest run (in
  memory) for deriving all granularities of that run. If a later pass (multi-level
  re-derivation, §2 summaries) needs them again, either persist them keyed by
  `(document, embedding-model)` or budget the re-embedding cost explicitly — never
  assume a persistent token-embedding cache that has not been built.
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
- **Propositionizing = decontextualize.** Resolve references, attach qualifiers, split
  compound claims.
  Example: "He argued it was insufficient" → "Smith argued the 2019 flood-defense
  budget was insufficient."
- **Why.** Atomic propositions retrieve better than raw sentences (which break on
  unresolved references) and better than passages (too diffuse).
  (Technique: *Dense X Retrieval*, Chen et al., 2023.)
- This step is also the front half of fact extraction: the same transformation
  that makes a span retrievable makes it graph-ready.

### 3.1 Extraction faithfulness — hardening the perception layer

Extraction is the one place the LLM would otherwise *dispose* rather than *propose*:
it bakes a *reading* of the text into a node treated as ground truth. Provenance
guarantees traceability to a span; it does **not** guarantee the proposition faithfully
represents that span. A dropped negation, a flattened hedge, or a mis-attributed claim
becomes a confidently-wrong atom with perfect provenance to a span that says the
opposite — invisible to audit without re-reading. So extraction gets its own hardening
discipline, parallel to the edge-judgment disciplines of §8. The principle is identical:
the LLM proposes a reading; the engine treats it as defeasible, scored, verifiable, and
overridable.

**Preserve epistemic operators as structured fields, never flattened into text.** A
proposition is not just a string; it carries (§10): **polarity** (asserted / negated),
**modality** (categorical / probable / possible / hypothesized), **attribution /
evidentiality** (asserted by the document, reported speech, or a named source's claim),
**quantifier scope**, and **epistemic class** (observation/measurement vs testimony/event
vs judgement/interpretation — see below). "The operator claimed the bearing probably
didn't fail" → {content: bearing failure; polarity: negated; modality: probable;
attribution: operator-claim; class: judgement}. These fields change how the fact reasons —
a negated claim supports the opposite hypothesis; a hedged or attributed claim carries
less intrinsic weight — so losing them is a silent corruption, not a stylistic detail.

**Observation vs judgement — and: extract observations, don't inherit conclusions.**
`epistemic_class` is orthogonal to modality (a *categorical* claim can still be a
*judgement*) and it governs how much source credibility matters (§9.1): an objective
**observation** ("the rolling surface shows particle indentations", a vibration reading)
stands largely source-independently — its risk is fabrication/error, checked by
corroboration and verification, not by interest-discounting — whereas a
**judgement/interpretation** ("therefore it was an assembly fault") is heavily
credibility- and interest-weighted. The consequence is a hard rule, and it is the whole
system's stance applied to source material: **extract a source's observations as facts,
but ingest the source's conclusions as defeasible, credibility-weighted judgement-claims
— never as facts — and re-derive the conclusion from the observations.** Take the
supplier's measurements; treat its "assembly fault" verdict as a low-credibility
judgement to be weighed; let the engine derive cause itself. Observational facts are the
strong grounding anchors (§12); the judgement layer — sources' conclusions, expert
interpretations, *and the system's own conclusions* — is uniformly defeasible.

**Reference binding is a separate, scored decision — not resolved invisibly.** Detecting
that a mention needs a referent is robust; choosing *which* entity is error-prone, so it
is split out (as sign is split from magnitude in §8). A `Mention` is bound to a canonical
entity by a defeasible, confidence-bearing **`REFERS_TO`** edge (§10), resolved through a
scoped cascade mirroring candidate generation (§5.1): local discourse antecedent → an
entity already in the graph for that box → an entity in the domain-pack taxonomy (§9) →
unresolved. Keeping the mention and binding as graph objects makes the resolution
auditable ("'it' → bearing-3, 0.6"), overridable (§10.3), and revisable — a later
re-binding propagates through belief revision like any other change. Use a dedicated
coreference model for local anaphora and entity-linking for definite descriptions;
**LLM attention weights are not a faithfulness signal** (attention is not a faithful
explanation of the output) and are used, at most, to *generate* candidate antecedents,
never to score them.

**Confidence comes from consistency and verification, not verbalized self-report.**
Reuse the §8 machinery: multi-sample extraction (stable extractions are high-confidence;
unstable ones flagged), and an **extract-then-verify** pass. **Agreement must be
computed within identical epistemic-field partitions** — embedding similarity cannot
distinguish polarity (a claim and its negation embed nearly identically, typically
above any usable equivalence threshold), so equivalence clusters are formed only
among candidates sharing `polarity` (and `epistemic_class`); a near-identical pair
that splits across polarities is a **negative** consistency signal — the extractor
is unstable on the claim's direction — and must drive agreement *down* and mark the
proposition provisional, never be averaged into a single high-agreement cluster.
Multi-sample also requires nonzero sampling temperature, or N identical samples make
agreement trivially perfect; the configuration must enforce this, not document it — an entailment/NLI check
that the source span actually supports the proposition *with its polarity and modality*
(the perception-layer analogue of ensemble contradiction). Verification catches both
hallucinated content (not in the source) and distortion (operator dropped).

**Three confidence types, kept distinct.** A proposition carries a **faithfulness
confidence** (does it represent the source?), separate from **source credibility** (is
the source reliable? — from box/tier, §9) and from **evidential strength** (how much does
it bear on a hypothesis? — the edge, §8). They fail differently and gate differently;
collapsing them reintroduces the entanglement that sign/strength/significance and
Layer A/B exist to avoid.

**Leave uncertainty open; gate by stakes.** Below a calibrated binding/faithfulness
threshold the reference is *not* silently resolved and the proposition is **marked
provisional and quarantined from high-stakes downstream use** — it may exist but must not
drive a strong move (e.g., a `REFUTES` that overturns a hypothesis) until confirmed, the
same logic as the ensemble gate on refutation (§7.2). The threshold is **stakes-
dependent**: a reference feeding a high-significance refutation needs higher confidence
than one feeding a minor corroboration. Provisional propositions and ambiguous bindings
are routed to the expert-triage queue (high value-of-information items) and confirmed via
soft override (§10.3); ambiguity is represented, not forced — as with uncertain level
attachment (§14) and surfaced cyclic regions (§13).

## 4. Indexing

Three indexes over the same shared offsets — no duplicated text.

- **Dense** — the cached embeddings, for semantic similarity.
- **Sparse lexical** — an exact-token term index. Catches exact tokens that
  embeddings blur: names, codes, acronyms, rare jargon. Word-frequency keywording
  belongs here, **not** in the graph. **Implementation honesty:** the self-hosted
  engine is Postgres FTS (`simple` tsvector + GIN), and `ts_rank` is **neither
  TF-IDF nor BM25** (no IDF term, no document-length normalization). Exact-token
  *recall* — the job above — is unaffected; *ranking* semantics are not BM25's, so
  nothing downstream may be tuned against BM25 score assumptions. True-BM25
  Postgres extensions (ParadeDB `pg_search`, VectorChord-BM25) are AGPL and would
  need the same service-edge isolation as MinerU (§1); adopt one only if rank
  fusion over FTS measurably under-ranks in Trial A1.
- **Graph nodes** — see §5.

Retrieval is **hybrid** (dense + sparse): semantic recall plus exact-match
precision. **Fusion is rank-based (Reciprocal Rank Fusion)** — never a weighted
sum of raw scores, because cosine and `ts_rank` live on incomparable scales; RRF
is insensitive to score semantics, which is exactly what the FTS caveat above
requires.

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

**Observations ground; judgements are re-derived, not inherited.** A Fact's
`epistemic_class` (§3.1) separates objective observations (strong grounding anchors,
largely source-credibility-independent) from a source's judgements/conclusions. The
system ingests a source's *observations* as facts but treats the source's *conclusions*
as defeasible, credibility-weighted judgement-claims and forms its **own** deductive/
inductive conclusions from the observations — it never inherits a source's verdict as a
fact. All conclusions are defeasible, but two distinct weighting paths apply and must not
be conflated: an **ingested** source judgement is weighted by conditional source
credibility (§9.1); a **system-derived** conclusion has no source to credit — it is
weighted by Layer B confidence over its well-founded support (§12). Observations are the
bedrock either way.

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

### 5.2 Entity resolution — which mentions are the same entity

`REFERS_TO` (§3.1) binds a mention to a canonical entity; this subsystem decides what
the canonical entities *are*. It is foundational and error-prone: **under-merging**
("the HSS bearing" / "bearing 3" / "it" kept separate) fragments evidence so support
never accumulates, and **over-merging** conflates distinct entities, manufacturing false
contradictions and transferring support spuriously. Because anchoring to a taxonomy *is*
entity linking, resolution quality also bounds abstraction-level quality (§14). It is a
subsystem, not a checkbox.

**Identity is a defeasible, revisable assertion, not a permanent id.** Two entities are
"the same" only via a scored **`SAME_AS`** edge (§10); the canonical entity is the
`SAME_AS`-connected component, and reasoning aggregates evidence at the component level.
Consequently **merge and split are ordinary belief-revision operations** — a merge
asserts a `SAME_AS`, a split retracts one — carried by the Layer A/B, bitemporal, and
override machinery (§12, §7.4, §10.3). Over-merging is therefore *recoverable*: split the
edge and the relationships it created are re-evaluated automatically.

**Resolution is a cheap→expensive cascade, like candidate generation (§5.1).** Block
candidates cheaply (shared tokens, embedding neighbourhood, shared type/box,
taxonomy-anchor) → score same-entity candidates on **relational/contextual** evidence
(shared facts, roles, attributes — *not* attention, and similarity only for blocking) →
resolve into components. It runs **continuously**, not as one upfront pass: identity
confidence updates as facts accumulate, so an early-ambiguous "bearing 3" can be resolved
later when context arrives (part of the loop's *revise* step, §11).

**Anchoring canonicalizes.** When a mention entity-links to a domain-pack taxonomy node
(§9), that node *is* the canonical identity — anchoring solves resolution for in-taxonomy
entities, and the same anchor-first / induce-fallback logic as §14 applies. The taxonomy
is simultaneously the abstraction hierarchy and the entity-resolution authority.

**Asymmetric errors, asymmetric default.** Under-merge yields false negatives (missed
connections); over-merge yields false positives (fabricated relationships), which corrupt
reasoning more. So the default is **conservative: auto-merge only above a high confidence
bar; below it keep entities separate but record a *candidate-merge* link** so the
fragmentation stays visible and the evidence bridgeable without committing identity.
Uncertain identity is represented, not forced (as in §3.1, §14). Candidate merges route
to the expert-triage queue and are confirmed via soft override (§10.3).

**Resolution and contradiction detection form a self-correcting loop.** If
`find-contradiction` (§6) fires and the conflict exists only because two facts hang off a
merged entity, that is evidence the `SAME_AS` was wrong — its confidence is lowered and it
is queued for split-review. Over-merges announce themselves as contradictions. To stop the
loop ping-ponging (merge → contradiction → split → re-merge), merge/split has
**hysteresis**: a split that resolved a contradiction raises the bar for re-merging that
pair, and a pair that flips more than a bounded number of times is frozen and **surfaced
as an unstable identity for expert decision** rather than flipped again — the same
"surface the unstable region, don't loop on it" discipline used for evidential cycles
(§7.2, §12).

**Scope.** Resolution respects box/pack scope: within a source box, resolve locally;
cross-box identity (`SAME_AS` spanning boxes) is established during an investigation and,
like any cross-box edge, belongs to the working box (§9). Cross-domain ambiguity (a
"valve" in plumbing vs the heart) is disambiguated by which domain pack is active.

## 6. Realization — storage, orchestration, interface

Borrowed pattern: the **enricher-orchestrator** skeleton from flowsint (an OSINT
graph tool — select a node, run an async operator, stream new nodes/edges into the
graph). The skeleton is reused; flowsint's OSINT operator catalog (DNS, WHOIS,
breach lookups) and its monotonic assumptions are discarded.

**Operators** replace flowsint's enrichers. Each takes a node and expands the
graph, runs async (slow LLM/retrieval work), and streams results to the UI:

- `extract` — span → propositions → facts (actors, objects). Includes the §3.1
  faithfulness steps: emit structured epistemic fields (polarity / modality /
  attribution / scope), bind `Mention`s via `REFERS_TO`, and run `verify` before a
  proposition is trusted.
- `verify` — proposition × span → faithfulness confidence. An entailment/NLI check that
  the span supports the proposition *with its polarity and modality* (§3.1); disagreement
  flags the proposition `provisional`.
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
- **AGE property indexes are mandatory, not an optimization.** AGE stores
  properties in an `agtype` column; without explicit indexes every
  `MERGE`/`MATCH` on `id` or `box` is a **sequential scan of the label table**.
  *Implemented (migration `0007`, G0.R2), shaped by what the planner actually
  emits:* a property-map filter (`{id: 'x'}`, `{box: 'b'}`) compiles to the
  **agtype containment operator** `properties @> …`, so each vertex label carries
  one **GIN index on `properties`** (backing id-lookup, box-scoped MATCH, and
  ad-hoc filters at once) — the originally-specified btree expression index on
  the `id` access would exist and never be chosen. Edges join on their graphid
  endpoint columns, so each edge label carries **btree on `start_id`/`end_id`**.
  Bitemporal as-of range indexes are deferred to their Phase 5 consumer (no as-of
  query shape exists yet to verify against). Index *use* must be verified with
  `EXPLAIN` through the actual query path (the `cypher()` SQL wrapping has sharp
  edges) — existence is not use; the verification test asserts the index appears
  in the plan (`tests/integration/test_age_label_indexes.py`). Shipped before
  Phase 2's continuous entity-resolution lookups (Trial C3 pairs with it).
- **PostgreSQL** (same instance) holds source text, span offsets, auth, and
  metadata as relational tables; `pgvector` holds the dense embedding index.
  Provenance resolution (Span → text) is therefore a local join, not a cross-engine
  hop, and belief-revision's multi-row updates ride Postgres MVCC.
- **Network analysis** is not done in the database. Extract the (small)
  per-investigation working subgraph and run **igraph** (or NetworkX): centrality
  (betweenness / PageRank) for load-bearing facts, community detection
  (Louvain / Leiden) for sub-arguments, weighted pathfinding for hypothesis support.
  (Community structure is associative co-grouping — it is *not* the part-whole
  partonomy of §14 and must never substitute for it.)
  This assumes working sets fit in memory — true at investigation scale; if a single
  graph ever reached millions of nodes, in-database analytics would have to be
  reconsidered. Transitive reachability for retraction uses Postgres `WITH RECURSIVE`
  rather than a graph traversal.

**Module split** (mirrors flowsint): types / core-orchestrator / operators / api
(with real-time event streaming) / app (node-expansion canvas, kept performant on
large graphs). Orchestration uses an open-source task queue; the LLM is a
swappable component (hosted API for the MVP, open-weight self-hosted model when
full self-hosting is required).

### 6.1 Cost and incrementality

The reasoning layer is LLM-heavy and multi-sample, so cost must be controlled by
design, not hoped away. The target scale is modest — an investigation is on the order
of tens of dense documents against a large but **static** reference corpus — and the
discipline below keeps both initial and incremental cost bounded (not exponential).

- **Two regimes, amortized.** Reference-corpus processing (the industry knowledge,
  domain packs) happens **once**: embedded, propositionized, extracted, anchored, then
  persisted and reused read-only across all investigations (§9). Only the
  investigation's own case documents are processed per investigation. Expensive
  reference passes are amortized, not repaid each time.
- **Content-addressed caching.** Extraction and adjudication outputs are cached keyed by
  content (+ model version); unchanged spans/propositions/edges are never re-inferred.
  "Embed once" extends to "extract once" for static content.
- **Cheap symbolic re-propagation, unbounded.** On any change, Layer A/B and the QBAF
  recompute only the **delta-affected sub-graph** (the transitive dependents), with **no
  LLM calls** — so it runs freely on every change and the cascade is linear in the
  affected region, never exponential.
- **Expensive LLM re-inference, value-gated.** Re-running an LLM operator on an affected
  region happens only when **value of information** (§11.1) says it could change the
  conclusion; a well-determined region far from any decision boundary is left alone even
  though it is downstream of the change. The same VoI scoring gates the machine's
  re-inference budget that ranks expert attention.
- **Budget-bounded mode.** Under a fixed budget, spend LLM calls in VoI order and stop at
  the budget or when VoI drops below threshold; un-inferred regions are flagged
  provisional, not silently dropped — a "good-enough on a budget" conclusion.

The one operation that can cascade is a **reference-corpus update**, which revises
dependent working conclusions; this is delta-scoped (only conclusions derived from the
changed reference facts) and value-gated like any other change, so it stays bounded.

## 7. Non-monotonic layer — what flowsint does not have

flowsint's enrichers are deterministic and its graph only grows: a DNS record is a
verified fact that never has to be retracted. Reasoning operators are fallible, and
a newly extracted fact can overturn an earlier conclusion. Four additions follow.

### 7.1 Edge confidence

`supports` / `refutes` / `derived-from` carry a strength, not a boolean.

### 7.2 Hypothesis state machine

Each hypothesis is recomputed — supported / unsupported / refuted — from its current
incoming evidence and the confidences on those edges. A flip to `refuted` requires the
**ensemble gate** (multi-sample LLM + symbolic + temporal agreement), never a single
judgment. The gate is **structural in the writer**: a computed `refuted` the ensemble does
not authorise is *held* at the hypothesis's prior state with a `pending_refutation` flag
(surfaced as a finding, §13), never silently flipped or dropped — `refuted` is unreachable
without an authorising gate decision.

### 7.3 Belief revision

When a new fact lands, every conclusion and hypothesis downstream of it is re-evaluated
and may be downgraded or retracted. The graph is non-monotonic: nodes can lose support,
not only gain it.

### 7.4 Bitemporal record

Never hard-delete. Each fact and edge carries event time and ingestion time with a
validity window; superseded facts are invalidated, not removed, preserving "what did we
believe at time T." This is the Graphiti/Zep pattern; we implement it directly on our own
edges (open-source, schema stays ours) rather than adopting it as a dependency — it
handles validity windows, not adjudication, which layers on top.

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
  source-reliability discounting (routing note, decided G4.3: source credibility is
  routed into edge `significance` per §9/§10, not applied inside the judge — the
  judge runs at identity reliability so the three quantities stay separate); (4)
  fuse correlated/conflicting evidence with cumulative or averaging fusion — never
  raw Dempster's rule under conflict.
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
- **Dependency/retraction: two layers (§12).** Truth maintenance certifying
  **well-founded** support (least fixpoint grounded in base facts; Counting for acyclic
  regions, DRed/Backward-Forward or clingo for recursive/cyclic ones, Differential
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
1. **Retraction core (Layer A).** Well-founded support grounded in base facts:
   Counting over acyclic regions, DRed (over-delete then re-derive) or clingo over
   `DERIVED_FROM` cycles; a conclusion survives iff it re-grounds in base facts, so
   unfounded cycles are dropped (§12).
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
*refuting* ones, which test the dissimilar-refuter problem — before adjudication;
(f) does fact→referent level attachment agree with human labels (anchored vs induced).
**Evaluate against domain gold answers with controlled answer ordering — not
LLM-as-judge headline scores**, which carry large position/length bias. Goal: learn
the real cascade behavior, the confidence-calibration gap, the candidate-recall gap,
and the level-attachment accuracy before hardening any layer.

**This synthetic experiment proves mechanisms, not efficacy.** Two further checks gate
the full build. (1) **Beat a cheap baseline (go/no-go):** the thin end-to-end system must
show material lift over plain RAG, agentic RAG, and expert+search on the *differentiator*
axes — contradiction/refuter handling, retraction on an overturning fact, traceability,
and calibration (an easy-question tie is fine; the value is where RAG is weak). If it
cannot, stop and rethink, keeping only the components an **ablation** shows carry the
value. (2) **Climb the validity ladder:** synthetic (mechanisms) → a *retrospective real
closed case* (messy evidence, known outcome — ecological validity) → prospective/live
expert use. Never claim efficacy from the synthetic gate alone.

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

**Domain packs — how the system models different domains.** The schema has two
layers. The **epistemic schema is fixed and domain-agnostic**: facts, actors,
objects, deductive/inductive conclusions, hypotheses, and the evidential/provenance
edges (§10). The **domain layer is pluggable**: the entity *types*, the part-whole
taxonomy (§14), and the domain inference rules vary by domain and are not hardcoded.
A **domain pack** packages the domain layer as one (or a few) reference/schema-tier
boxes containing: the part-whole taxonomy (ISO 14224, a bill of materials, FMA, an
org chart…), the domain entity-type ontology, optional domain rules (the clingo
deductive/defeasible rules of §8), and an optional **reference hypothesis set** (known
failure modes, FMEA / differential-diagnosis libraries, ISO 14224 failure modes) used to
seed candidate answers for a Task (§11.2). An investigation **activates** the packs it needs;
cross-domain work activates several. Multi-domain support is therefore an
*instantiation* of the tier/box model, not a new subsystem — a domain is a set of
reference boxes. **Reliability is a function of anchoring:** the system is reliable in
a domain to the extent that domain's authoritative structure can be plugged in;
where it cannot, the system degrades to lower-confidence induced structure under
human review (§14). Conflicting taxonomies across packs are resolved by the same
tier/entrenchment and override machinery as any other conflict.

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

### 9.1 Governance — sensitivity, credibility, versioning, cold-start

**Sensitivity propagates; access is a projection.** A `sensitivity` label (a lattice:
public < internal < confidential < restricted, plus compartment tags, §10) sits on
Documents/Spans and **propagates to derived nodes as the least upper bound (max) of
their antecedents** along `DERIVED_FROM`/`EVIDENCED_BY` — the information-flow
high-water-mark, computed over the provenance graph Layer A already maintains. Access
control is then another projection (like the §14 frontier): a viewer sees only nodes
at-or-below their clearance and within their compartments; a visible conclusion whose
provenance they are not cleared for is shown with the trail redacted. Consequence:
**auditability is relative to clearance** — the full source-to-conclusion trace holds for
someone cleared for the whole chain. Boxes are the natural compartment unit.

**Credibility is conditional, not a flat scalar — and it is gated by epistemic class.**
For an **observation/measurement** (§3.1) credibility is a minor factor: the claim stands
on its merits and its risk (fabrication/error) is checked by corroboration and
verification, not by interest-discounting. For a **judgement/interpretation** it is
central. So credibility applies where it matters: a source's effective credibility on a
*judgement* = its **base reliability** (the box `reliability_prior`) **× a modifier from
the alignment between the claim and the source's interest**. A self-serving claim is
discounted (a bearing supplier blaming transport/assembly for its own component's
failure); a claim *against* the source's interest is boosted (an admission against
interest is a recognized reliability signal). The typical interest/role patterns are
domain knowledge and live in the **domain pack** ("component suppliers are self-interested
regarding their components"); per-claim alignment is LLM/expert-flagged, defeasible,
overridable, and logged. Conditional credibility feeds the proposition's evidential weight
and the edge `significance` — it is the *credibility* term in the faithfulness/credibility/
strength separation (§3.1, §8).

**Adversarial sources.** Conditional credibility is the first defense; three more compose
from existing machinery. *Independence-aware corroboration*: corroboration counts only
across **independent** sources — two that trace to one origin are one (reuses provenance /
entity resolution), defeating copy-flooding. *Track-record revision*: a source caught in a
refuted claim has its credibility lowered for its other claims (credibility is
belief-revised by performance within the investigation). *Coherence + triage*: isolated or
contradicted self-serving claims are down-weighted by the QBAF and are high-VoI for expert
review (§11.1). Honest residual: a sophisticated adversary planting internally-consistent,
independently-sourced false evidence can still fool the system — deception-robustness is
unsolved; the design makes deception harder and more visible, not impossible.

**Pack/taxonomy versioning.** Because level is *derived* (§14), a taxonomy change
re-derives levels automatically; only anchors can break. Packs are **versioned and
bitemporal** (§7.4); each anchor is **stamped with the pack version it bound against**; a
pack update is a bitemporal supersession (so "what did we conclude under pack v1" stays
answerable) that triggers **delta-scoped, value-gated belief revision** (§6.1) on
dependent anchors. A removed/merged/moved taxonomy node is handled like an entity
merge/split one level up (§5.2); a broken anchor falls back to induced/relative level and
flags for review (the §14 coverage policy).

**Cold-start.** A novel domain (the high-value case) lacks a pack, so anchoring — the
reliable path — is least available exactly when it is most wanted. Two responses: the
system runs in **induce-mode** (hierarchy/levels induced per §14, everything provisional,
high triage load — graceful degradation, not failure); and it **bootstraps a pack via
promotion** — confirmed entities, hierarchy, and hypotheses are promoted (gated) into a
nascent reference pack, so each investigation in a novel domain makes the next cheaper.
Honest expectation: the *first* investigation in a truly novel domain carries the highest
human cost.

## 10. Schema (the contract)

Stated once here so it is not reconstructed from prose elsewhere. This is the
authoritative data model; other sections defer to it. Nodes and edges below are AGE
vertices and edges; raw text, offsets, and the vector index are ordinary relational
tables in the same Postgres instance, joined to the graph by id.

### Node labels

- **`Document`** — an ingested source. Properties: `id`, `box` (id of the box it
  belongs to), `uri`/`title`, `ingested_at`, `sensitivity` (lattice label +
  compartment tags, §9.1). The source's reliability and interest are carried by its
  `Box` (§9.1); a `Document` may override `source_interest` only when a box spans
  multiple sources. Raw text and offsets live in PostgreSQL, keyed by `Document.id`;
  the graph holds the node, not the text.
- **`Span`** — a contiguous source range, the unit of provenance. Properties: `id`,
  `document_id`, `start` and `end` (character offsets into that document's text),
  `level` (segmentation level that produced it), and optional **`layout` {page, bbox}**
  from the parse front-end (§1) for visual provenance — resolving a claim to a region on
  the original page image, not just a character offset. A Span is the *only* thing that
  points at raw text; every provenance reference resolves to one or more Span ids.
- **`Proposition`** — a decontextualized atomic statement (the §3 unit). Properties:
  `id`, `text` (the rewritten, self-contained form), `box`; the structured epistemic
  fields (§3.1) `polarity` ∈ {asserted, negated}, `modality` ∈ {categorical, probable,
  possible, hypothesized}, `attribution` (document / reported-speech / named-source),
  `scope` (quantifier scope notes), `epistemic_class` ∈ {observation, testimony,
  judgement} (orthogonal to modality; gates how much source credibility applies, §3.1/
  §9.1); and `faithfulness` ∈ [0, 1] (calibrated confidence that the proposition
  represents its span — distinct from source credibility and from evidential strength),
  plus a `provisional` flag set when faithfulness or a binding is below the
  stakes-dependent threshold. Linked to the Span(s) it came from. Propositions are
  first-class nodes, not free text on other nodes.
- **`Mention`** — a surface reference in a span ("it", "the bearing", "bearing 3")
  awaiting binding (§3.1). Properties: `id`, `surface`, `box`, and the Span it occurs
  in. Bound to a canonical entity by a `REFERS_TO` edge; an unbound or low-confidence
  Mention marks dependent propositions `provisional`.
- **`Actor`**, **`Object`** — entities. **These are nodes, not properties of a
  fact.** Properties: `id`, `label`, `type`, `box`. Identity is resolved into
  components via `SAME_AS` (§5.2), not by destructive id reassignment; reasoning
  aggregates evidence at the `SAME_AS`-component level.
- **`Fact`** — an asserted state of affairs extracted from a Proposition. Properties:
  `id`, `box`, `tier`, plus the bitemporal and confidence fields below. A Fact links
  to its Actors/Objects (by `INVOLVES`) and to its Proposition and Span(s) (by
  `EVIDENCED_BY`).
- **`DeductiveConclusion`**, **`InductiveConclusion`** — derived claims. Same
  property set as Fact. Inductive is provisional by definition.
- **`Hypothesis`** — a claim under test. Properties: those of Fact, plus
  `state` ∈ {supported, unsupported, refuted} (computed, never set by hand) and
  `acceptability` (the real-valued QBAF strength it was computed from). For
  presentation, `acceptability` bands into a graded verdict (true / plausible /
  implausible / false), §11.2.
- **`Task`** — the intentional node: an investigative goal / framing question (§11.2).
  Properties: `id`, `question` (text), `type` ∈ {causal, normative, existence,
  comparative, …}, `answer_state` ∈ {open, partially-answered, answered, abandoned},
  `box`. **Distinct from epistemic nodes — a Task is *answered*, never adjudicated
  true/false.** The root Task is the user's question; sub-Tasks form a decomposition
  tree.
- **`Box`** — the registry node (§9). Properties: `id`, `tier`
  ∈ {schema, reference, case, working}, `version`, `source`, `reliability_prior`,
  `source_interest` {role, stake} (the §9.1 input for conditional credibility),
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
- **`REFERS_TO`** — Mention → the canonical Actor/Object it denotes (§3.1). Defeasible,
  confidence-bearing, provenanced, and overridable like any other edge; may carry
  multiple candidate targets when ambiguous. Resolved through a scoped cascade (local
  antecedent → in-graph entity → taxonomy → unresolved); confidence from consistency +
  verification, not attention.
- **`SAME_AS`** — Actor/Object ↔ Actor/Object: an identity assertion (§5.2). Scored,
  defeasible, provenanced, bitemporal, overridable; `state` ∈ {candidate, confirmed}.
  The `SAME_AS`-connected component is the canonical entity; asserting/retracting an edge
  is a merge/split handled as belief revision. A `candidate` edge keeps entities separate
  but bridgeable pending confirmation.
- **`DECOMPOSES_INTO`** — Task → sub-Task: the investigation decomposition tree (§11.2).
  LLM-proposed, value-gated, expert-editable.
- **`ADDRESSES`** — Hypothesis → Task: this hypothesis is a candidate answer to the Task.
- **`RELEVANT_TO`** — Fact/Conclusion → Task: in-scope evidence for the Task.
- **`PART_OF`** — Actor/Object → the containing Actor/Object (a roller part-of a
  bearing part-of an assembly). **Typed and split:** `directPartOf` records each direct
  decomposition step (intransitive); `partOf` is its transitive closure, with roll-up
  restricted to the transitivity-safe component-integral subtype (§14). Carries a
  meronymy-type tag, and — like every edge — confidence, provenance (anchored vs
  induced), bitemporal fields, and overridability. Forms a partial order / DAG (an
  entity may belong to more than one parent). This is the part-whole hierarchy of §14 —
  distinct from the chunk levels of §2, from `DERIVED_FROM`, and from associative
  community structure (§6). A reasoning node's **abstraction level is derived**, not
  stored: it is the position of the node's subject-role `INVOLVES` entity in this
  order (§14).

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
    Concretely (decided G4.3): `significance = tier_weight(tier) ×
    effective_credibility` (§9.1); the §8 judge runs at identity source-reliability
    so `strength` stays the pure connection judgment — credibility enters here,
    never the strength discount. `tier_weight` is uniform 1.0 until the §8
    experiment calibrates it.
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
- **`sensitivity`** (any content node) — a lattice label + compartment tags (§9.1).
  On a derived node it is the **least upper bound** of its antecedents' sensitivity
  (propagated along provenance, never set below an antecedent). Drives access-control
  projection and clearance-relative auditability.
- **`credibility`** (effective, per claim) — **derived, never a flat stored scalar.**
  Computed = the source's base `reliability_prior` (§9) × `f(interest_alignment,
  epistemic_class)` (§9.1) — self-serving discounted, against-interest boosted, and
  near source-independent for observations. The stored *inputs* are `reliability_prior`
  + `source_interest` (on `Box`), `epistemic_class` (on `Proposition`), and a derived
  per-claim `interest_alignment` annotation; effective credibility is computed at
  use-time (optionally materialized for query perf, recomputed on input change — like
  abstraction level, §14). Belief-revised by the source's track record. Distinct from
  `faithfulness` (§3.1) and from edge `strength` (§8).

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
frame                  set the Task (the framing question) and its type; seed
                       hypotheses from decomposition, the domain pack's reference
                       hypothesis set, and the expert (§11.2). Scopes everything below.
        ↓
assemble working set   select active boxes: case-evidence box(es) + relevant
                       reference boxes (incl. the Task's domain pack(s)); create one
                       working box. Seed each edge's confidence prior from its source
                       box's reliability_prior.
        ↓
retrieve               hybrid (dense + sparse) over the active boxes only, scoped
                       by box id and by the Task; pull candidate facts/propositions.
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
                       semantics; propagate retraction by well-founded support
                       (Counting on acyclic regions, DRed/clingo on cycles, §12). Only the
                       affected sub-graph is touched.
        ↓
analyse                extract the working subgraph into igraph/NetworkX for network
                       analysis: centrality (load-bearing facts), community
                       detection (sub-arguments), hypothesis-support pathfinding.
        ↓
triage                 rank the "needs-human" items by value of information (§11.1) so
                       the expert reviews the highest-leverage uncertainties first.
        ↓
sufficiency            is the Task answered to threshold? If yes (or the VoI of further
                       work is below threshold), stop expanding (§11.2). This is the
                       principled stopping point — without a Task there isn't one.
        ↓
present                the answer to the Task: its addressing Hypotheses with state and
                       acceptability (banded true / plausible / implausible / false),
                       each with its SUPPORTS/REFUTES subgraph and resolved provenance
                       (Span → source text). Rendered at the audience's abstraction level
                       via a cut through the PART_OF hierarchy — a mixed-level frontier,
                       abstract where that suffices and detailed where the signal is
                       (§14).
        ↺
revise                 new evidence — or an expert soft override (§10.3) — re-enters
                       at "expand"; downstream conclusions and hypotheses are
                       re-evaluated, not recomputed wholesale.
```

A result is never a bare answer: it is always the claim plus its evidence subgraph
plus the provenance trail, so a human can audit why the system holds it.

### 11.1 Review triage — value of information

The system generates far more nodes and edges than an expert can review, so the
human-in-the-loop loop only closes if attention is *directed*. Triage ranks every
"needs-human" item by **value of information**: `VoI ≈ leverage × uncertainty ×
significance` — review what would most change the conclusion, weighted by how unsure we
are and what is at stake. The ranking is computed from quantities the system already
maintains; **no LLM runs in the ranking**, so it stays cheap relative to the review it
schedules.

**Leverage** — how much the conclusion depends on the item. Cheap structural proxies:
centrality (§6) and Layer A support-counts (a conclusion with a *single* derivation is
fragile; sole-support edges / articulation points are high-leverage). Precise measure:
perturb the item (flip a sign, zero a strength) and re-run the QBAF on the local
subgraph — the swing in acceptability is the leverage. Sharpest form is
**decision-relevance**: does perturbing it reorder the top hypotheses or flip a state?
Leverage concentrates where two hypotheses are near-tied. Cost discipline: the cheap
structural proxies rank the **whole** queue; QBAF perturbation (which re-solves the
semantics per item and is *not* free at scale) runs only on the **top-k** the proxies
surface. "No LLM in the ranking" holds throughout — perturbation is symbolic — but the
two-tier split is what keeps triage genuinely cheap.

**Uncertainty — and which kind.** Aggregate the deliberately-separated confidence types:
faithfulness (§3.1), reference binding (§3.1), entity resolution / candidate merge
(§5.2), edge-strength calibration (§8), source reliability (§9); expert-confirmed items
(§10.3) drop to ~zero. Because the types are separate, triage tells the expert **what
judgment is needed** — confirm a referent, weigh evidence, accept/reject a merge — not
merely where to look.

**One ranked queue.** Every needs-human signal feeds it: provisional propositions
(§3.1), ambiguous bindings (§3.1), candidate merges (§5.2), unresolved/cyclic regions
(§13), and override-reconciliation prompts (§10.3). VoI orders them into a single
budgeted top-N stream; this *is* the prioritization of the expert-triage queue referenced
throughout. The same VoI score also gates the machine's **re-inference budget** (§6.1):
expensive LLM re-analysis runs only where it could change the conclusion.

**Guard against confident-wrong, not only uncertain.** Pure uncertainty-ranking would
miss the worst failure mode — a high-confidence atom that is wrong. So triage also
surfaces **fragile-confidence** items (a confident conclusion resting on a single
unverified, low-faithfulness source) and **conflicting-confidence** items (a confident
claim contradicting an entrenched belief), even when the math says "sure."

**Robustness.** Cheap and deterministic (scales, adds no cost); dynamic but **batched**
(re-rank between batches, not per click, so it doesn't thrash); **budgeted** (top-N, never
an unranked dump); graceful fallback to significance + recency when VoI cannot
discriminate; and **auditable** — each item shows its VoI decomposition (what turns on it,
which uncertainty, what is at stake), per principle 9. VoI is defined relative to the
current decision — which, when a Task is set (§11.2), is *answering that Task*.

### 11.2 Task framing and goal-directedness

Without a stated goal the loop tends to build the network exhaustively rather than
*answer a question*, which compounds the cost (§6.1) and triage (§11.1) problems. So the
investigation's goal is a first-class input — a distinct **intentional layer** over the
epistemic graph. The two layers have different semantics and must not be conflated: a
**`Task` is *answered*** (open → answered); a **`Hypothesis` is *adjudicated*** (its
truth-state computed from evidence). The Task layer frames and scopes; it never becomes
"true."

**`Task` (the intentional node, §10).** The framing question, with a *type* (causal —
"why did X fail?"; normative — "was maintenance negligent?"; existence — "did Z happen?";
comparative; …) and an *answer-state* (open / partially-answered / answered / abandoned).
The root Task is the user's question; it **`DECOMPOSES_INTO`** sub-Tasks — a tree in which
a sub-Task is either a sub-question or an adjudication goal ("establish hypothesis H").
A `Hypothesis` **`ADDRESSES`** a Task; facts/conclusions are **`RELEVANT_TO`** a Task (the
scope).

**Hypothesis framing — seeding from three sources.** Candidate answers to a Task come
from (a) decomposition (the LLM proposes), (b) the **domain pack's reference hypothesis
set** (known failure modes, FMEA/diagnosis libraries, ISO 14224 failure modes — §9), and
(c) the expert. Once seeded they are adjudicated by the existing QBAF; **true / plausible
/ implausible / false is the hypothesis acceptability, banded** — not a parallel truth
system.

**Task type selects the reasoning mode.** A normative Task *deductively* applies a
reference-tier standard to the facts; a causal Task *abductively / inductively* infers the
best-supported cause; an existence Task confirms/refutes a single hypothesis. The type
determines which operators and rules apply and what counts as an answer.

**Goal-directedness scopes the whole loop — and supplies the stopping criterion.** The
Task scopes retrieval (evidence relevant to it), candidate generation (pairs bearing on
its hypotheses), and hypothesis formation (answers of its type); it defines the *decision*
that VoI (§11.1) and the re-inference budget (§6.1) optimize for; and it provides the
**stopping point** — expansion halts when the Task is answered to threshold or the VoI of
further work falls below threshold. This is the direct cure for exhaustive network-building.

**Goal-directedness must not lose completeness.** Scoping to a Task trades against the
guarantee that exhaustive building gave for free: that you would not *miss the unframed
hypothesis*. A well-scoped investigation can confidently answer the wrong question —
"was maintenance negligent?" scopes away from "the part was counterfeit." Three guards
keep coverage: (a) **abductive hypothesis generation** — high-significance evidence that
*no* current hypothesis explains spawns a new candidate hypothesis (and, if needed, a
sub-Task), even outside the current frame; (b) a **residual exploration budget** that
never falls to zero, so some retrieval and candidate generation always runs unscoped;
(c) a periodic **undirected sweep** of the working set as a completeness check. The Task
focuses effort; these keep it from becoming blinkered. Surfacing an *unexplained*
high-significance observation is itself a first-class finding.

**Two cautions.** Decomposition must be **value-gated and expert-editable** — an LLM that
recursively explodes every Task into dozens of sub-Tasks just relocates the
exhaustive-expansion problem into the Task tree; decompose where it helps answer the
question, and let the expert prune/edit it (principle 6). And the Task layer is an
**optional overlay**: the system can still run undirected (exploratory "build the network"
mode); a Task makes it goal-directed.

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
A node is supported iff it has **well-founded support**: it is in the least fixpoint
built from **base facts** (grounded by `EVIDENCED_BY`, or axiomatic domain rules) and
closed under "if all antecedents of some derivation are supported, the conclusion is."
This founded semantics is the whole point of the layer — it is what distinguishes a
grounded mutual-support pair (both also reach base → correctly kept) from an
**unfounded cycle** (nodes that support only each other after their external grounding
is retracted → correctly dropped). A per-node integer **derivation-support count** is
the incremental *implementation* of this fixpoint, not its definition.

**Cycle safety is a correctness requirement, not a performance detail.** Plain
**Counting is correct only for acyclic (non-recursive) derivations** — on a cycle it
will keep a count positive from the cycle's own members and fail to retract an
unfounded loop (the classic unfounded-set bug). So: use Counting for the acyclic
majority, but route nontrivial **strongly-connected components of the `DERIVED_FROM`
graph** to a cycle-safe algorithm — **DRed (Delete–Rederive): over-delete everything
reachable from the retracted support, then re-derive only what re-grounds in base
facts**, so an over-deleted ungrounded cycle never returns (Backward/Forward is the
alternative). For recursive or non-monotonic regions, hand foundedness to **clingo**:
unfounded-set elimination is exactly what ASP stable-model / well-founded semantics
computes, so clingo's role widens from consistency checks to founded-support
computation under recursion. The scale path, **Differential Dataflow / DBSP (Feldera)**
fed by Postgres CDC, does recursive retraction correctly by construction. This layer
answers only "is this **well-founded**-supported, and by how many derivations" — never
"how strongly."

**Layer B — confidence valuation over an absorptive, ω-continuous semiring (owns
strength).** On the nodes Layer A certifies as **well-founded**-supported, compute
confidence as a least fixpoint over the **Viterbi semiring `([0,1], max, ·, 0, 1)`** —
multiply confidences along a rule body (conjunction), take the max across alternative
derivations (disjunction), i.e. best-derivation confidence. (Use the Gödel /
fuzzy `max-min` semiring instead if the degrees are ordinal rather than
probability-like.) Both are idempotent and absorptive, so the fixpoint is
well-defined and **convergent even on cyclic derivation graphs**, and is
double-counting-free across multiple derivations by construction. Recompute only on
the delta-affected sub-graph Layer A reports as changed. **Never use the
probabilistic sum-product semiring here** unless derivations are provably
independent — it double-counts and can diverge on cycles.

**The Viterbi-vs-Gödel choice is an explicit Phase-3-entry decision, not a default.**
Viterbi `max-·` carries a structural **depth bias**: confidence decays geometrically
with derivation depth (five 0.9-confidence steps → 0.59 regardless of evidence
quality), so deep, careful derivations are punished relative to shallow ones and the
meaning of any acceptability band (§11.2) varies with chain length. Gödel `max-min`
is depth-neutral — a chain is as strong as its weakest link — which matches the
ordinal, ordering-driven use the QBAF makes of these scores. Decide with a fixture
demonstrating both behaviours on a deep chain vs a shallow one before Layer B is
built; if Viterbi is kept, the banding must be made depth-aware (strictly more
machinery). The same depth-compounding concern applies at the perception layer,
where `faithfulness = verify × agreement` and credibility multiply in series —
Trial A5 should evaluate `min` as the combiner alongside the product.

**Foundedness gates confidence — and that ordering is what makes cycles safe.** Layer B
*converges* on a cycle but convergence is not foundedness: alone it would assign a
confidence to an ungrounded loop. Because Layer A decides membership *before* Layer B
scores it, and membership now means *well-founded*, the unfounded cycle is dropped by
Layer A and never receives a Layer B confidence. (This is **derivation**-cycle safety,
`DERIVED_FROM`; it is distinct from **evidential**-cycle handling — mutual
`SUPPORTS`/`REFUTES` — which is Layer B convergence plus QBAF oscillation detection,
§13. Different graphs, different fixes.)

**Why two annotations from day one.** Every fact/edge carries both an integer
support-count (Layer A) and a `[0,1]` confidence (Layer B). Layer A *should* count
multiplicities — that is how it knows when support hits zero. Layer B must *not* —
idempotence is what prevents inflation. Conflating them reintroduces the failure
this split exists to avoid.

**The contract handed to adjudication.** Layer B's `[0,1]` confidence is the clean
strength consumed as a node's intrinsic/base score by the QBAF gradual semantics
(§8). That is the seam between propagation and adjudication: Layer A decides
**well-founded** membership, Layer B scores it, QBAF adjudicates supports/refutes over
those scores.

**Escalation paths (only if needed).** If confidence ever needs genuine subtractive
incremental maintenance, escalate to a **valuation algebra with division**
(Kohlas–Wilson) rather than abusing a semiring. If calibrated *probabilities* are
required (not ordinal confidence) and derivations are correlated, move to
probabilistic-database lineage + knowledge compilation and accept the cost
(#P-hard). Neither is in scope for the MVP.

**Termination of the composed loop.** Each layer converges in isolation (Layer A is a
least fixpoint; Layer B is absorptive and ω-continuous; QBAF gradual semantics converges),
but the *composition with retraction feedback* — a `REFUTES` retracts a fact that was
supporting the refuter — has no closed-form convergence guarantee. So the loop is run with
an **iteration bound and oscillation detection**: on reaching the bound without a fixpoint,
the unstable sub-region is **surfaced as a finding** (an unresolved/circular region with
its subgraph), never silently re-iterated (§7.2, §13). And re-inference is made
**monotonic in effort**: an expensive LLM re-inference runs **at most once per
evidence-state** of a region (the content-addressed cache key is extended with the
region's state hash, §6.1), and the re-inference budget strictly decreases — so the
VoI↔re-inference loop cannot churn the same region.

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
- **Well-founded support under derivation cycles is a correctness requirement** (§12).
  "Supported" means well-founded — re-grounding in base facts — not a positive local
  count, which a `DERIVED_FROM` cycle can manufacture and fail to retract (the
  unfounded-set bug). Plain Counting is correct only for acyclic regions; recursive/
  cyclic SCCs use DRed (over-delete then re-derive) or clingo (ASP foundedness), and
  DBSP at scale. This is distinct from evidential-cycle handling (Layer B / QBAF). It is
  deterministic correctness, so it is a must-pass test (grounded vs ungrounded cycle
  fixtures), not a tune-to-fit gate.
- **Negation / aggregation in rules breaks plain provenance.** Monus-based difference
  provably violates natural axioms, so if rules use negation, restrict to stratified
  negation evaluated by clingo (or a recursion-safe IVM algorithm — *not* plain
  Counting), or adopt the dual-indeterminate provenance approach. Do not assume a
  simple "subtract" works.
- **The two-layer model is a synthesis, not a packaged result.** No published system
  combines ℤ-count truth-maintenance + Viterbi confidence + QBAF hand-off as one
  named architecture; we are integrating across the database-provenance, IVM, and
  argumentation literatures. The integration seams are ours to validate.
- **LLM→QBAF weight mapping is unstandardized** (also in §8 custom work). Turning a
  calibrated LLM judgment into a base score and an attack/support edge has no
  reference recipe; it must be designed and validated against the planted-corpus
  experiment.
- **Goal-directedness is the keystone for cost and triage** (§11.2). Without a Task the
  loop has no principled stopping point and builds exhaustively, compounding §6.1 and
  §11.1. The Task supplies the decision that VoI and the re-inference budget optimize for,
  and the answered-to-threshold stopping criterion. The residual risk is the *Task tree
  itself*: auto-decomposition that explodes into dozens of sub-Tasks relocates the
  exhaustive-expansion problem, so decomposition is value-gated and expert-editable. The
  Task layer is an optional overlay — undirected exploration remains possible.
- **Cost is bounded at investigation scale, given the §6.1 discipline.** At tens of case
  documents against an amortized static reference corpus, with content-addressed caching,
  delta-scoped symbolic re-propagation (no LLM), and VoI-gated re-inference, both initial
  and incremental cost stay bounded and non-exponential. The residual is reference-corpus
  *update* cascades — themselves delta-scoped and value-gated. The assumption to watch is
  that investigations stay at this scale; a genuinely corpus-scale, continuously-ingesting
  deployment would reopen this and push more work to the DBSP scale path.
- **Expert-attention triage decides whether the loop closes at all** (§11.1). The system
  produces far more than an expert can review; without value-of-information ranking,
  review is arbitrary (whatever is on screen) and the human-in-the-loop story fails. VoI
  is computed cheaply from existing quantities, but its *effectiveness* must be shown:
  that VoI-ordered review reaches the correct conclusion in fewer reviews than cheaper
  baselines (centrality-only, confidence-only, random). If it cannot beat those, the
  machinery is not justified and a simpler ordering should be used. Sharpens once the
  investigation question is explicit (the goal-directedness direction).
- **Entity resolution silently bounds everything** (§5.2). Under-merging fragments
  evidence (false negatives); over-merging fabricates contradictions and transfers
  support spuriously (false positives); and because anchoring is entity linking,
  resolution quality caps abstraction-level quality (§14). The defeasible-`SAME_AS` /
  reversible-merge design, the conservative under-merge default with candidate links,
  and the contradiction→split-review loop reduce the damage and make over-merges
  recoverable — but resolution remains a primary error source, measured on its own gate
  (precision = over-merge control, recall = under-merge control) and bounded by routing
  candidate merges to expert review.
- **Extraction faithfulness is a foundation risk, not just an edge risk** (§3.1). The
  whole graph reasons over extracted propositions; a dropped negation, flattened hedge,
  or mis-attribution is a confidently-wrong atom with perfect provenance. The §3.1
  discipline (structured epistemic fields, separate scored reference binding,
  extract-then-verify, consistency-based confidence, stakes-gated quarantine) reduces
  this but does not eliminate it — verification is itself model-judged, and rare
  faithful-looking distortions will pass. Measured by a dedicated faithfulness gate
  (entailment, negation/modality preservation, coreference accuracy), and the residual
  is bounded by routing provisional propositions to expert review, not by assuming
  extraction is correct. Attention weights are explicitly *not* used as a faithfulness
  signal.
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
- **Part-whole acquisition is reliable only when anchored** (§14). Anchoring to a
  domain pack's taxonomy (entity linking) is the reliable path; text-induced meronymy
  is domain-fragile and the weakest off-the-shelf step; relative ordering is heuristic.
  Levels carry confidence + provenance (anchored vs induced) and a coverage policy
  governs the fallback. Transitivity is unsafe except for the component-integral
  subtype, so `partOf` roll-up is restricted (§10, §14). Flat-embedding cosine and
  lexical concreteness are the *wrong geometry/proxy* for level — use partonomy depth +
  intrinsic IC, or box/ConE embeddings. Mis-attached levels silently distort every
  level-based view, so hierarchy edges are defeasible and expert-overridable.
- **The mixed-level frontier and the fact→referent level operator are the concentrated
  novelty/risk.** No published system drives per-region resolution by evidence
  significance, nor derives a fact's relative level from its referent. Build the
  frontier on the degree-of-interest framework but own the significance weighting as
  research with evaluation gates (§14); a single global resolution parameter provably
  cannot substitute for it.
- **The storage engine under this schema density is unproven** (§6). Apache AGE is far
  less battle-tested than Neo4j, and every node/edge now carries provenance, two
  annotations, sensitivity, conditional credibility, bitemporal validity, and overrides.
  AGE + this density + bitemporality is a real viability risk that "engine: chosen" should
  not paper over; benchmark it early (it belongs in the scale trials), and treat
  **bitemporality as scoped** — clearly needed for boxes/overrides/packs, not obviously for
  every `SAME_AS` edge. If AGE cannot carry it, the fallback is a separate graph store at
  the cost of the single-engine simplicity.
- **Extraction now carries many structured fields per proposition** (polarity, modality,
  attribution, scope, epistemic_class, faithfulness, plus credibility/interest) — each an
  error source, and the verifier shares a model family with the extractor (correlated
  error). The faithfulness gate measures the damage but does not remove it; an
  **independent verifier (different model family)** is the concrete mitigation, and the
  per-proposition field burden is a standing reliability tax to watch.
- **Governance residuals (§9.1).** Sensitivity propagation makes *auditability relative to
  clearance* — a partially-cleared viewer cannot see the full trace, which is correct but
  weakens the "trace everything" guarantee for them. Conditional credibility depends on an
  *interest model* that is domain-authored and imperfect. Adversarial robustness is
  bounded: independence-aware corroboration + track-record revision + triage raise the
  cost of deception but a sophisticated, independently-sourced fabrication can still pass.
  Cold-start means the *first* investigation in a novel domain is expensive (induce-mode +
  heavy review) before promotion bootstraps a pack. None are solved; all are bounded and
  surfaced.
- **Validation realism — the synthetic gate is necessary but not sufficient, and the
  complexity must be justified** (§8). A planted corpus proves mechanisms under
  controlled conditions; it has low external validity, so efficacy requires climbing to a
  retrospective real closed case and then prospective use. Separately, the whole approach
  must **beat a cheap baseline** (RAG / agentic RAG / expert+search) on the differentiator
  axes early, as a go/no-go — it is entirely possible that a fraction of the system
  captures most of the value, which a component ablation is designed to find. Building the
  full system before this check is the expensive failure mode.
- **Evaluation must be bias-controlled.** LLM-as-judge carries large position and
  length bias (reported graph-RAG win-rates have collapsed by half under correction),
  so the validation gate (§8) evaluates against domain gold answers with controlled
  ordering, not LLM-as-judge headline scores. Level attachment is gated on
  inter-annotator agreement before automation; inferred-level embeddings are gated on
  depth-recovery correlation before they are trusted.

## 14. Abstraction and the part-whole hierarchy

Knowledge arrives at different abstraction levels, and facts attach at different
levels of a domain's part-whole structure: "the gearbox had a bearing failure"
attaches at *gearbox*; "the rolling surface shows particle indentations" attaches at
*roller*. The system must model, search, and **present knowledge at the level relevant
to the audience and the region** — management abstract, domain experts fine — and the
most significant knowledge does not sit at one uniform level (principle 8 applied to
abstraction).

**Level is relative, derived, and a property of the referent — not the sentence.**
There is no absolute abstraction level of a fact; level is only meaningful relative to
a hierarchy. So level is not a stored scalar on a fact. It is the position of the
fact's **primary referent** (its subject-role `INVOLVES` entity) in the `PART_OF`
order (§10). Derived this way it stays correct as the hierarchy is refined and is
relative by construction. Ambiguity (a fact that could attach at *bearing* or
*gearbox*) is represented as uncertain/multiple attachment, not forced to one value.

**Three hierarchies in the reasoning graph, kept distinct — and a fourth, associative
one.** (1) chunk/abstraction levels of *text* (§2); (2) the *derivation* structure
(`DERIVED_FROM`); (3) this *part-whole* structure of *entities* (`PART_OF`). A fourth,
**associative community structure** (the Leiden communities of §6, used to cluster
sub-arguments) is *not* a part-whole hierarchy and must never be substituted for one —
community structure is statistical co-grouping, partonomy is compositional containment.
The four serve different purposes; conflating any two corrupts the views built on them.

**`PART_OF` is typed, and only one subtype is transitivity-safe.** Part-of is not
uniformly transitive across meronymy types (Winston/Chaffin/Herrmann; Keet & Artale).
The system distinguishes a transitive **`partOf`** from an intransitive
**`directPartOf`** (the W3C pattern): `directPartOf` records each direct decomposition
step; `partOf` is its transitive closure, and **abstraction roll-up (ancestor views)
runs only along the transitivity-safe component-integral / functional-complex subtype**
(gearbox⊃shaft⊃bearing⊃roller). Member-collection, portion-mass, and stuff-object
meronymy are tagged and excluded from blanket roll-up, or wrong aggregations leak into
management-level views.

**Acquiring the `PART_OF` hierarchy — anchor first, induce only as fallback.** Text-
induced meronymy is the weakest off-the-shelf step (domain-fragile), so anchoring is
the primary path and induction is the gap-filler, with the level's *confidence and
provenance* recording which path produced it:

1. **Anchor to the active domain pack (primary, reliable).** When a domain pack (§9)
   supplies a taxonomy (ISO 14224, BOM, FMA, org chart), attaching a fact is **entity
   linking**: resolve its referent to a taxonomy node and read the level off. This is
   why the taxonomy lives in the reference/schema tier — durable, reusable, pluggable
   per domain. High-confidence attachment.
2. **Induce meronymy from text (fallback, for out-of-taxonomy referents).** The LLM
   extraction pass emits `directPartOf` candidates from compositional noun phrases
   ("high speed shaft locating bearing"), "Y of X", possessives, "part of", "consists
   of". Lower-confidence, human-review-gated, merged with anchored structure.
3. **Relative ordering (last resort, when no parent is named).** Containment cues +
   co-occurrence/degree asymmetry (general entities co-occur with many, specific with
   few) + the §2 chunk-level prior. Enough to order two entities, which is all
   "relative level" requires.

**Coverage policy:** measure the fraction of referents that anchor to the active
pack(s). High coverage → anchoring is the level-assignment mechanism. Persistently low
coverage → the domain pack is inadequate; escalate to induction + review and treat
inferred levels as provisional, never as reliable.

**Estimating level concretely.** For an anchored entity, level = its depth in the
partonomy, plus an intrinsic information-content score (Seco-style, from subtree size —
structure-only, no corpus) for a continuous generality value. For an out-of-taxonomy
entity, learn relative generality with **box embeddings** (volume ≈ generality) or
**ConE** when carrying both is-a and part-of (it models multiple heterogeneous
hierarchies at once). **Do not rank level by embedding cosine** (similarity is not
containment) **nor by lexical concreteness** (perceptual concreteness ≠ taxonomic
generality). A node's abstraction level is then derived from its primary referent
(its subject-role `INVOLVES` entity, §10); ambiguity is represented as
uncertain/multiple attachment, not forced.

**Audience views are projections, not new data.** A view is a *cut* through the
`PART_OF` DAG: management rolls facts/hypotheses up to coarse ancestor entities (with
summaries); experts drill to leaves. A projection operator over the existing graph.

**The mixed-level frontier (the hard, novel part).** The most significant knowledge is
not at one uniform level, and a single global resolution parameter provably cannot
render very-different scales at once (the community-detection resolution limit). So the
cut is not horizontal: a frontier through the DAG sits **high where abstract facts
suffice and deep where detail carries the signal**. Build it on the established
*degree-of-interest* framework (Furnas; van Ham & Perer's search/show-context/expand-
on-demand) but **replace "a-priori importance − distance" with "evidence significance −
distance"** — descend a subtree only where finer facts are load-bearing (high evidence
weight, contested, where live hypotheses sit), weighted by the §6 network-analysis
signals. The significance-weighted frontier and the fact→referent level operator are
the two genuinely novel pieces (no published system does either); they are research,
resourced with evaluation gates, not routine engineering — see §13.

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
- [ ] **Review-triage VoI tuning** — the leverage/uncertainty/significance weighting and
  the batch/re-rank cadence (§11.1); and proving VoI-ordered review beats cheaper
  baselines. Measured by the triage-efficiency trial.
- [ ] **Entity-resolution tuning** — the auto-merge confidence bar (the under/over-merge
  asymmetry), the relational-evidence scorer, and continuous re-resolution cadence
  (§5.2). Measured by the resolution gate (precision/recall).
- [ ] **Extraction faithfulness tuning** — the consistency/verification thresholds and
  the stakes-dependent quarantine cutoff (§3.1); coreference approach (dedicated model
  vs LLM) per domain. Measured by the faithfulness gate.
- [ ] **Candidate-generation recall tuning** — the funnel thresholds (§5.1),
  especially k for embedding k-NN and the coarse-level cutoff, and how to measure
  refuter recall specifically. Experiment-driven.
- [ ] **Mixed-level frontier rendering** — the per-investigation, per-audience policy
  for where to drill vs stay abstract over the `PART_OF` DAG (§14); novel, weighted by
  §6 significance.
- [ ] **Part-whole hierarchy acquisition quality** — meronymy-extraction noise,
  relative-ordering heuristics, and whether to add order/hyperbolic embeddings for
  level (§14, §13). Measure attachment accuracy on the experiment corpus.
- [ ] **Governance policies (§9.1)** — the sensitivity lattice + compartments and
  clearance-filtered projection; the conditional-credibility interest model (domain-pack
  authored) and track-record revision; pack-version migration of anchors; and the
  cold-start induce-mode + promotion-bootstrap workflow. Build-time + per-deployment.
- [ ] **Domain-pack coverage & portability** — for each target domain, what fraction of
  referents anchor to its pack's taxonomy (the §14 coverage policy), and how packs
  (taxonomy + entity types + rules) are authored, versioned, and validated across
  domains (§9).
- [ ] **Re-evaluation trigger policy** — eager propagation vs lazy recompute-on-read,
  and the propagation bound.
- [ ] **Layer B semiring (Viterbi vs Gödel)** — decide at Phase 3 entry with a
  depth-bias fixture (§12); if Viterbi is kept, make acceptability banding
  depth-aware. Same question for the perception-layer combiner (`×` vs `min`) in
  Trial A5.
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
- Extraction faithfulness — coreference resolution (span-ranking / neural coref) and
  entity linking for reference binding; NLI/entailment for proposition-vs-span
  verification; negation and modality/hedge detection. Attention is not a faithful
  explanation (Jain & Wallace 2019; Wiegreffe & Pinter 2019) — not used to score
  bindings.
- Entity resolution — blocking + scored pairwise/cluster resolution (record linkage /
  Fellegi–Sunter lineage; Swoosh collective ER); cross-document coreference; incremental
  and reversible merge/split. Relational/contextual evidence over surface similarity.
- Sparse + dense hybrid retrieval — BM25 with embeddings.
- Cheap keyphrase extraction — TextRank, YAKE, KeyBERT.
- Candidate generation / pair pruning — blocking in entity resolution; approximate
  nearest-neighbour retrieval; link prediction. Coarse-to-fine pruning over the §2
  abstraction levels.
- Part-whole hierarchy — meronymy typology (Winston/Chaffin/Herrmann); reasoning-grade
  modeling (Keet & Artale; W3C `partOf`/`directPartOf`); entity linking to domain
  taxonomies (ISO 14224, BOM, FMA/SNOMED). Level estimation: partonomy depth + intrinsic
  information content (Seco et al.); box embeddings and ConE for out-of-taxonomy
  generality (not cosine, not concreteness). Hypernymy/taxonomy induction as adjacent
  tooling (Hearst patterns; TaxoLLaMA). GraphRAG as an optional construction scaffold —
  but its community hierarchy is not a partonomy.
- Mixed-level / audience-adaptive views — degree-of-interest and expand-on-demand
  (Furnas; van Ham & Perer); semantic zoom. Significance-weighted frontier is novel
  (§14).
- Enricher-orchestrator pattern and graph storage — flowsint (OSINT graph tool).
- Graph network analysis — igraph / NetworkX over extracted per-investigation
  subgraphs (centrality, community detection, pathfinding).
- Incremental retraction & founded support — DRed / Backward-Forward (recursion-safe;
  Counting only for acyclic views); Differential Dataflow / DBSP (Feldera). Foundedness
  / unfounded-set elimination — well-founded semantics (Van Gelder, Ross & Schlipf) and
  ASP stable-model semantics via clingo.
- Review triage — value of information / expected value of perfect information
  (decision-theoretic); sensitivity analysis over the argumentation graph; active
  learning for query prioritization. Computed from existing annotations, no LLM in the
  ranking (§11.1).
- Document parsing (front-end) — layout analysis + reading-order + OCR + table/figure/
  formula extraction with per-element bounding boxes (MinerU, default; Docling/Marker as
  alternatives). Swappable behind a fixed contract; AGPL → run as a service (§1).
- Goal-directed inquiry — hypothesis-driven / abductive reasoning; inference to the best
  explanation; task decomposition. Hypothesis seeding from domain failure-mode libraries
  (FMEA, ISO 14224) and differential-diagnosis sets (§11.2).
- Governance — information-flow / multi-level security label lattices (Bell–LaPadula
  high-water-mark) for sensitivity propagation; conditional credibility / admission
  against interest (evidence law); independence-aware corroboration (§9.1).
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
