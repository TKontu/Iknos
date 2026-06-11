# Phase 2 — Graph Construction (Nodes)

**Goal:** populate the reasoning graph with typed nodes — facts, actors, objects —
organized into boxes/tiers, each traceable to source and logged.

**Depends on:** Phase 0 (schema, audit log, box registry), Phase 1 (propositions).
**Architecture refs:** §5 (reasoning graph nodes), §9 (tiers & boxes), §6 (operators,
`extract`), §10 (`INVOLVES`, `EVIDENCED_BY`), §10.1 (action log).

## Entry criteria (do not start node extraction before these)

*Added by the 2026-06 review (`review_2026-06_architecture_plan.md`); each exists
because Phase 2 is where its absence turns from latent to expensive.*

- [x] **AGE property indexes merged (G0.R2, `gap_phase_0_residual.md`).** Entity
      resolution runs continuous per-mention MERGE/MATCH lookups; without indexes every
      one is a label-table seq scan. **Done** — migration `0007_age_label_indexes`:
      GIN on `properties` per vertex label (the `@>` containment filter behind id +
      box + ad-hoc lookups) and btree on `start_id`/`end_id` per edge label
      (endpoint joins / `SAME_AS`/`partOf` traversal). Verified by `EXPLAIN` through
      the real `cypher()` path (`tests/integration/test_age_label_indexes.py`), not by
      index existence.
- [ ] **Trial C3 density benchmark run early** (`todo_trials.md` C3 — pulled forward):
      a synthetic graph at target schema density, on the four real query patterns,
      *before* building heavily on AGE. If AGE fails here, the fallback decision
      (separate graph store) must be made now, not after Phases 2–5 are built on it.
- [ ] **Quarantine enforcement lands with the first evidential edges (G1.6).** The
      `provisional` flag is already set per proposition; the §3.1 rule "provisional
      atoms cannot drive high-stakes moves (e.g. a `REFUTES`)" is enforced at
      edge-creation time — which begins in this phase. Until enforced, the flag is
      decorative.
- [x] **Polarity-aware agreement (G1.14) and the truncation guard (G1.13 slice 1)
      shipped** — Phase 2 consumes propositions and their faithfulness; both fixes
      change what reaches it. *(Shipped in #32 — `feat(ingest): G1.13 slice 1
      truncation guard + G1.14 polarity-aware agreement`.)*
- [x] **Structured table payload available (G1.18)** if table extraction is in this
      phase's scope — the "rows/cells → propositions with column semantics" task
      below has nothing to read without it. *(Shipped in #40 — `feat(ingest): G1.18
      structured table payload in the parse wire contract`. The payload is now available;
      the Phase-2 rows/cells→propositions consumer that reads it is not yet built.)*

## Boxes & tiers (§9)

- [x] Operationalize the **tier** axis (schema → reference → case → working) as the
      reasoning/entrenchment ordering; `tier` resolved from `Box`, override allowed.
      *(G2.1 — `boxes/serde.resolve_tier(box, override)`; the `Tier` order is the §9
      entrenchment ordering, consumed by `extract` when it stamps Facts.)*
- [x] Operationalize the **box** axis (lifecycle/provenance unit): create, version,
      set reliability prior, status (active/deprecated). *(G2.1 — `boxes/registry`:
      `create_box` (create-only `valid_from`), `deprecate_box`; `Box.version`/
      `reliability_prior`/`status`. Metadata *editing* (changing reliability) is the
      later governance/soft-override concern.)*
- [~] **Source vs working** boxes: source boxes append-on-ingest; one mutable working
      box per investigation (full lifecycle wiring in Phase 6). *(G2.1 — the source side
      ships: `boxes/serde.case_box` builds an append-on-ingest case box (the `extract`
      write target). The mutable per-investigation working box is Phase 6, as scoped.)*
- [x] Box-scoped management operations (SQL by `box`); reasoning reads across active
      boxes by tier + reliability. *(G2.1 — `boxes/registry.list_boxes` (by box/tier/
      status) and `active_boxes_by_tier` (joint read across active boxes, ordered by
      `reliability_prior` desc) — the §9 "reasoning reads across active boxes" query.)*
- [x] Reference boxes are mostly TBox (rules/taxonomies); case boxes are ABox
      (observations) — reflect in how extraction populates each. *(Reflected: the domain
      pack loader (G0.7 `domain/loader`) populates **reference** boxes with the TBox
      taxonomy; `extract` (G2.2) populates **case** boxes with ABox Facts.)*
- [~] **Domain packs (§9):** activate the investigation's domain pack(s); resolve the
      domain entity-type ontology + part-whole taxonomy + optional **reference hypothesis
      set** (known failure modes / FMEA / diagnosis libraries, for Task seeding §11.2)
      from them. The epistemic schema stays fixed; only the domain layer comes from packs.
      Cross-domain = multiple packs active. *(G0.7 — `domain/loader.load_pack` resolves a
      pack's entity-type ontology + part-whole taxonomy (Objects + `directPartOf`/`partOf`)
      into a reference Box; `Box.status == active` is the activation flag and `list_active_packs`
      the lookup. **Investigation-scoped** activation (an `ACTIVATES` edge from the root Task)
      and the **reference hypothesis set** are Phase 6 seams.)*

## Node extraction (the `extract` operator, §6)

- [x] `extract`: proposition → `Fact` with `Actor`/`Object` nodes. Actors and objects
      are **nodes, not properties** (§5/§10). *(G2.2 — `core/extract.py`: one Fact per
      proposition; entities are fresh `Actor`/`Object` vertices. Dedup is G2.3.)*
- [x] **Entity resolution as a subsystem (§5.2), not a dedup pass.** Identity via scored
      `SAME_AS` edges; the canonical entity is the `SAME_AS`-connected component;
      reasoning aggregates evidence at component level (no destructive id reassignment).
      *(G2.3 — `core/resolve.py`: scored `SAME_AS` with `SameAsState`; `canonical_components`
      reads the connected components. Thin slice; seams below.)*
  - [x] Cascade like candidate generation: block cheaply (shared tokens, embedding
        neighbourhood, type/box, taxonomy-anchor) → score on **relational/contextual**
        evidence (shared facts/roles/attributes; similarity for blocking only; **not
        attention**) → resolve into components. *(G2.3 — `block_candidates` (shared-token,
        same-kind) → deterministic relational `score_pair` → `components`. Embedding-neighbourhood
        and taxonomy-anchor blocking signals deferred — need an entity-embedding store / G2.4–G2.5.)*
  - [x] **Anchor canonicalizes:** a mention that entity-links to the domain-pack taxonomy
        takes that node as its canonical identity (anchor-first, §9/§14). *(G2.8 slice 1 —
        `core/anchor.py`: the **entity-linking** subsystem — a scored, conservative
        `ANCHORS_TO` edge (case entity → taxonomy node; the direction **is** "anchor
        canonicalizes") via a deterministic lexical cascade, plus the `anchored_targets` read.
        G2.8 slice 2 — `resolve.anchored_components` **folds** the confirmed `ANCHORS_TO` map
        into the `SAME_AS` components and `resolve.canonical_components` reads it: a confirm-
        anchored component takes its taxonomy node as `canonical`, and two mentions sharing an
        anchor target fold into one entity (a `SAME_AS`-bridged multi-anchor conflict is
        surfaced, not auto-resolved). Belief revision on a re-anchor is the Phase-3 seam.)*
  - [x] **Conservative default:** auto-merge only above a high confidence bar; below it
        keep entities separate but record a `candidate` `SAME_AS` link (bridgeable, not
        committed). Route candidate merges to expert triage; confirm via override (§10.3).
        *(G2.3 — `decide`/`RESOLVE_CONFIRM_BAR`/`RESOLVE_CANDIDATE_BAR`; `confirmed` vs
        `candidate` state. Expert-triage queue routing is Phase 7.)*
  - [ ] **Merge/split as belief revision:** asserting/retracting a `SAME_AS` re-runs
        Layer A/B over the affected component (Phase 3); both are logged, bitemporal,
        reversible. *(Deferred → Phase 3; this slice writes edges, does not re-run reasoning.)*
  - [ ] **Contradiction→split-review loop:** when `find-contradiction` conflict exists
        only via a merged entity, lower the `SAME_AS` confidence and queue split-review.
        **Hysteresis:** a split raises the re-merge bar; a pair that flips more than a
        bounded number of times is frozen and surfaced as an unstable identity for the
        expert — never flipped again (§5.2). *(Deferred → Phase 4; needs `find-contradiction`.)*
  - [x] Scope by box/pack; cross-box `SAME_AS` belongs to the working box (§9). *(G2.3 —
        within-source-box resolution: the caller passes one box's entities. Cross-box
        `SAME_AS` in the working box is deferred to the investigation runtime, Phase 6.)*
- [x] **Reference binding (§3.1):** detect `Mention`s ("it", "the bearing", "bearing 3")
      as a step *separate* from binding; bind each to a canonical entity with a scored,
      defeasible `REFERS_TO` edge via the scoped cascade (local antecedent → in-graph
      entity → domain-pack taxonomy → unresolved). Use a dedicated coreference model +
      entity linking; **do not score bindings by attention.** Confidence from
      consistency + verification. Low-confidence/ambiguous bindings stay open (multiple
      candidates), mark dependent propositions `provisional`, and route to expert triage.
      *(G2.4 — `core/reference.py`: LLM **detection** only → deterministic lexical binding
      (no attention) → scored `REFERS_TO` with `BindingState`; ambiguous/unresolved stay
      open + mark the proposition `provisional`. Thin slice: the in-graph-entity cascade
      stage. Deferred seams — pronoun/local-discourse-antecedent + taxonomy-anchor stages,
      multi-sample/verify confidence, expert-triage queue (Phase 7), re-bind belief
      revision (Phase 3).)*
- [x] `INVOLVES` edges (fact → actor/object) with `role`; `EVIDENCED_BY` edges (fact →
      proposition/span). *(G2.2 — Fact `EVIDENCED_BY` its Proposition and each Span.)*
- [x] Seed each fact's source-reliability/`significance` prior from its box tier (§9,
      feeds Phase 4 edge significance). *(G2.6 — the source-reliability prior (box
      `reliability_prior`) is reachable Fact→Box and consumed by `effective_credibility`;
      `significance` is an SUPPORTS/REFUTES *edge* property, so it lands with those edges in
      Phase 4, not on the Fact.)*
- [x] **Conditional credibility (§9.1), gated by epistemic class:** for **observations**
      credibility is minor (checked by corroboration/verification, not interest-discount);
      for **judgements** effective credibility = box base reliability × claim-interest
      alignment (self-serving discounted; against-interest boosted). Source `interest`/role
      patterns come from the domain pack; per-claim alignment is LLM/expert-flagged,
      defeasible, logged. Distinct from faithfulness and strength. *(G2.6 —
      `core/credibility.py`: `effective_credibility` is **derived, never stored** — box
      reliability × an epistemic-class-gated `interest_modifier`; `effective_credibility_of`
      reads the stored inputs at use-time. The per-claim `interest_alignment`
      (`InterestAlignment`) slot exists on the Fact; the LLM/expert alignment-judging pass is
      a deferred seam, so it is `None`→`UNKNOWN` (identity) until then.)*
- [x] **Sensitivity (§9.1):** carry the source `sensitivity` onto facts; derived nodes
      inherit the max of antecedents (propagated in Phase 3/5). *(G2.6 — `extract` seeds a
      base Fact's `sensitivity` as the lub of its source Span(s) (`seed_sensitivity`); the
      `DERIVED_FROM` walk that propagates to conclusions is Phase 3/5.)*
- [x] Both annotations initialized: support-count and confidence (§12). *(G2.2 —
      `base_annotations`: `support_count=1` (one `EVIDENCED_BY` grounding), `confidence`
      seeded from faithfulness or the Viterbi identity `1.0`; the computed Layer-B value
      is the Phase-3 fixpoint.)*

## Part-whole hierarchy (§14) — abstraction levels

- [x] Build the `PART_OF` hierarchy over `Actor`/`Object` entities as **typed** edges:
      `directPartOf` (intransitive step) + `partOf` (transitive closure) with a
      meronymy-type tag; DAG; defeasible, provenanced, bitemporal, overridable. Restrict
      transitive roll-up to the component-integral subtype (§10/§14). *(G2.5 —
      `core/partwhole.py` + `edges.MeronymyType`/`is_transitive`: `transitive_closure` is
      cycle-safe (Kahn-isolates meronymy cycles, excludes+flags them) and component-integral-
      restricted; edges carry the type tag, two annotations, bitemporal.)*
- [~] **Anchor first (primary, reliable):** entity-link each referent to the active
      domain pack's taxonomy (ISO 14224, BOM, FMA…) and read the level off. Record
      attachment provenance = anchored, high confidence (§14). *(G2.8 slice 1 — the
      **entity-linking** half ships (`core/anchor.py`, scored `ANCHORS_TO` to the active pack
      taxonomy). "Read the level off" the anchored partonomy depth + stamping
      `AttachmentProvenance.ANCHORED` is slice 2, wiring `partwhole`'s derived-level read to
      follow a confirmed anchor into the pack's `partOf` order.)*
- [x] **Induce only as fallback (out-of-taxonomy referents):** the `extract` pass emits
      `directPartOf` candidates from compositional noun phrases ("high speed shaft
      locating bearing"), "Y of X", possessives, "part of". Lower confidence,
      human-review-gated; provenance = induced. *(G2.5 — `MeronymyInducer`: LLM detection
      from compositional cues → `directPartOf` with `provenance=induced`, `INDUCED_CONFIDENCE`.)*
- [ ] **Relative ordering (last resort):** containment cues + co-occurrence/degree
      asymmetry + the §2 chunk-level prior, when no parent is named. *(Deferred → §14 step 3.)*
- [x] **Coverage policy:** measure the fraction of referents that anchor to the active
      pack(s). High → anchoring is the level mechanism; persistently low → pack
      inadequate, escalate to induction + review and mark levels provisional (§14).
      *(G2.8 slice 1 — `anchor.EntityLinker.coverage` / `AnchorCoverage`
      (confirmed-anchored / total canonical entities) reads off the `ANCHORS_TO` edges; the
      Trial-A4 anchoring-coverage measurement and the pack-adequacy signal that gates whether
      levels are anchored or provisional.)*
- [~] **Level estimation:** anchored → partonomy depth + intrinsic IC (Seco, subtree
      size, structure-only); out-of-taxonomy → box embeddings (or ConE for joint
      is-a + part-of). Do **not** use embedding cosine or lexical concreteness as level
      proxies (§13). *(G2.5 — the **structure-only partonomy depth** (`derived_level` =
      ancestor count) ships; the intrinsic-IC refinement and box-embedding/ConE generality
      are deferred seams. Embedding cosine / lexical concreteness are correctly never used.)*
- [x] Attach each fact's **derived level** via its subject-role `INVOLVES` entity;
      represent ambiguous attachment as uncertain/multiple, not forced (§14). *(G2.5 —
      `fact_level`: subject-role referent's depth, canonicalized; several subjects → several
      levels, never forced.)*
- [x] Keep `PART_OF` distinct from the §6 community structure — community ≠ partonomy.
      *(G2.5 — `partOf` is compositional containment built from meronymy cues only; the §6
      Leiden community structure is a separate, later subsystem and is never substituted.)*

## Provenance & audit (cross-cutting, enforced here)

- [~] Every created node/edge has a non-empty provenance path to `Span`(s) (§10).
      *(G2.2 writes the path at creation — Fact `EVIDENCED_BY` Proposition + Span(s),
      entities box-tagged and named in the same Action; G2.7 makes the **Fact-anchored**
      path checkable (`audit_box_facts`). A *universal* per-node/edge crawler (every
      Actor/Object/edge proven independently) is a deferred seam — entities are reached
      through their Fact, so they inherit its provenance.)*
- [x] Every `extract` run emits an `Action` record: inputs (spans/propositions),
      outputs (node ids), model, sampling (§10.1). *(G2.2 — and every Phase-2 write
      operator does likewise: `resolve`, `reference`, `partwhole`, the box registry, and
      the pack loader each `record_action` at creation, via `provenance/action_log`.)*
- [x] Verify per-node auditability: from a `Fact`, reach its spans, source text, and
      producing `Action` (§10.2). *(G2.7 — `provenance/audit`: `fact_provenance` walks
      Fact → Proposition + Span(s) → resolved source text → producing extract `Action`;
      `audit_box_facts` is the box-level invariant (returns the Facts that fail, with the
      gap reasons). Backed by the migration-0009 partial functional index on
      `actions(outputs->>'fact')` so the reach-back stays O(log n).)*

## Exit criteria

- [x] Ingested propositions become a deduplicated graph of facts/actors/objects in the
      correct box and tier. *(G2.2 produces Facts + Actor/Object nodes boxed/tiered;
      "deduplicated" = the **non-destructive** `SAME_AS`-component identity of G2.3 — the
      canonical entity is the connected component, reasoning aggregates at component level.)*
- [~] No node or edge exists without provenance and an `Action` record. *(Enforced at
      creation by every write operator; **verified** for Facts by G2.7 `audit_box_facts`.
      The universal per-node/edge crawler that would make this fully checkable is the
      deferred seam noted under Provenance & audit.)*
- [x] Reference vs case knowledge can be loaded into distinct boxes and queried both
      separately (by box) and jointly (by tier). *(G0.7 pack loader → reference boxes;
      G2.2 extract → case boxes; G2.1 `list_boxes` (by box) + `active_boxes_by_tier`
      (jointly by tier).)*
- [~] Facts attach to a `PART_OF` hierarchy — anchored to a domain pack where coverage
      allows, induced+flagged otherwise — and a node's level resolves from its referent.
      *(G2.5 — the **induced+flagged** path and structure-only level ship; `fact_level`
      resolves a node's level from its subject-role referent. G2.8 slice 1 ships the
      **entity-linking** the anchored path needs (`ANCHORS_TO` + coverage); reading the level
      off the anchored taxonomy depth (`AttachmentProvenance.ANCHORED`) is slice 2.)*

## Phase risks / decisions

- Entity resolution is foundational and bounds everything downstream (§5.2): under-merge
  fragments evidence, over-merge fabricates contradictions, and it caps anchoring/level
  quality. Get the conservative default + reversible merge/split right; measured on its
  own gate (Trial A6).
- Keep extraction's typed `Actor`/`Object` as the node source — never fall back to
  word-frequency keywords for nodes (§4).
- **Anchoring coverage is the reliability driver across domains.** Text-induced
  meronymy is the weakest link; a domain works well only if its pack's taxonomy covers
  most referents. Measure coverage per domain; thin packs mean provisional levels +
  more expert review, not silent guessing (§14).
- Cross-domain entity ambiguity (a "valve" in plumbing vs the heart) is disambiguated
  by the active pack scope — verify dedup respects pack boundaries.
