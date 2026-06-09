# Phase 2 — Graph Construction (Nodes)

**Goal:** populate the reasoning graph with typed nodes — facts, actors, objects —
organized into boxes/tiers, each traceable to source and logged.

**Depends on:** Phase 0 (schema, audit log, box registry), Phase 1 (propositions).
**Architecture refs:** §5 (reasoning graph nodes), §9 (tiers & boxes), §6 (operators,
`extract`), §10 (`INVOLVES`, `EVIDENCED_BY`), §10.1 (action log).

## Boxes & tiers (§9)

- [ ] Operationalize the **tier** axis (schema → reference → case → working) as the
      reasoning/entrenchment ordering; `tier` resolved from `Box`, override allowed.
- [ ] Operationalize the **box** axis (lifecycle/provenance unit): create, version,
      set reliability prior, status (active/deprecated).
- [ ] **Source vs working** boxes: source boxes append-on-ingest; one mutable working
      box per investigation (full lifecycle wiring in Phase 6).
- [ ] Box-scoped management operations (SQL by `box`); reasoning reads across active
      boxes by tier + reliability.
- [ ] Reference boxes are mostly TBox (rules/taxonomies); case boxes are ABox
      (observations) — reflect in how extraction populates each.
- [ ] **Domain packs (§9):** activate the investigation's domain pack(s); resolve the
      domain entity-type ontology + part-whole taxonomy + optional **reference hypothesis
      set** (known failure modes / FMEA / diagnosis libraries, for Task seeding §11.2)
      from them. The epistemic schema stays fixed; only the domain layer comes from packs.
      Cross-domain = multiple packs active.

## Node extraction (the `extract` operator, §6)

- [ ] `extract`: proposition → `Fact` with `Actor`/`Object` nodes. Actors and objects
      are **nodes, not properties** (§5/§10).
- [ ] **Entity resolution as a subsystem (§5.2), not a dedup pass.** Identity via scored
      `SAME_AS` edges; the canonical entity is the `SAME_AS`-connected component;
      reasoning aggregates evidence at component level (no destructive id reassignment).
  - [ ] Cascade like candidate generation: block cheaply (shared tokens, embedding
        neighbourhood, type/box, taxonomy-anchor) → score on **relational/contextual**
        evidence (shared facts/roles/attributes; similarity for blocking only; **not
        attention**) → resolve into components.
  - [ ] **Anchor canonicalizes:** a mention that entity-links to the domain-pack taxonomy
        takes that node as its canonical identity (anchor-first, §9/§14).
  - [ ] **Conservative default:** auto-merge only above a high confidence bar; below it
        keep entities separate but record a `candidate` `SAME_AS` link (bridgeable, not
        committed). Route candidate merges to expert triage; confirm via override (§10.3).
  - [ ] **Merge/split as belief revision:** asserting/retracting a `SAME_AS` re-runs
        Layer A/B over the affected component (Phase 3); both are logged, bitemporal,
        reversible.
  - [ ] **Contradiction→split-review loop:** when `find-contradiction` conflict exists
        only via a merged entity, lower the `SAME_AS` confidence and queue split-review.
        **Hysteresis:** a split raises the re-merge bar; a pair that flips more than a
        bounded number of times is frozen and surfaced as an unstable identity for the
        expert — never flipped again (§5.2).
  - [ ] Scope by box/pack; cross-box `SAME_AS` belongs to the working box (§9).
- [ ] **Reference binding (§3.1):** detect `Mention`s ("it", "the bearing", "bearing 3")
      as a step *separate* from binding; bind each to a canonical entity with a scored,
      defeasible `REFERS_TO` edge via the scoped cascade (local antecedent → in-graph
      entity → domain-pack taxonomy → unresolved). Use a dedicated coreference model +
      entity linking; **do not score bindings by attention.** Confidence from
      consistency + verification. Low-confidence/ambiguous bindings stay open (multiple
      candidates), mark dependent propositions `provisional`, and route to expert triage.
- [ ] `INVOLVES` edges (fact → actor/object) with `role`; `EVIDENCED_BY` edges (fact →
      proposition/span).
- [ ] Seed each fact's source-reliability/`significance` prior from its box tier (§9,
      feeds Phase 4 edge significance).
- [ ] **Conditional credibility (§9.1), gated by epistemic class:** for **observations**
      credibility is minor (checked by corroboration/verification, not interest-discount);
      for **judgements** effective credibility = box base reliability × claim-interest
      alignment (self-serving discounted; against-interest boosted). Source `interest`/role
      patterns come from the domain pack; per-claim alignment is LLM/expert-flagged,
      defeasible, logged. Distinct from faithfulness and strength.
- [ ] **Sensitivity (§9.1):** carry the source `sensitivity` onto facts; derived nodes
      inherit the max of antecedents (propagated in Phase 3/5).
- [ ] Both annotations initialized: support-count and confidence (§12).

## Part-whole hierarchy (§14) — abstraction levels

- [ ] Build the `PART_OF` hierarchy over `Actor`/`Object` entities as **typed** edges:
      `directPartOf` (intransitive step) + `partOf` (transitive closure) with a
      meronymy-type tag; DAG; defeasible, provenanced, bitemporal, overridable. Restrict
      transitive roll-up to the component-integral subtype (§10/§14).
- [ ] **Anchor first (primary, reliable):** entity-link each referent to the active
      domain pack's taxonomy (ISO 14224, BOM, FMA…) and read the level off. Record
      attachment provenance = anchored, high confidence (§14).
- [ ] **Induce only as fallback (out-of-taxonomy referents):** the `extract` pass emits
      `directPartOf` candidates from compositional noun phrases ("high speed shaft
      locating bearing"), "Y of X", possessives, "part of". Lower confidence,
      human-review-gated; provenance = induced.
- [ ] **Relative ordering (last resort):** containment cues + co-occurrence/degree
      asymmetry + the §2 chunk-level prior, when no parent is named.
- [ ] **Coverage policy:** measure the fraction of referents that anchor to the active
      pack(s). High → anchoring is the level mechanism; persistently low → pack
      inadequate, escalate to induction + review and mark levels provisional (§14).
- [ ] **Level estimation:** anchored → partonomy depth + intrinsic IC (Seco, subtree
      size, structure-only); out-of-taxonomy → box embeddings (or ConE for joint
      is-a + part-of). Do **not** use embedding cosine or lexical concreteness as level
      proxies (§13).
- [ ] Attach each fact's **derived level** via its subject-role `INVOLVES` entity;
      represent ambiguous attachment as uncertain/multiple, not forced (§14).
- [ ] Keep `PART_OF` distinct from the §6 community structure — community ≠ partonomy.

## Provenance & audit (cross-cutting, enforced here)

- [ ] Every created node/edge has a non-empty provenance path to `Span`(s) (§10).
- [ ] Every `extract` run emits an `Action` record: inputs (spans/propositions),
      outputs (node ids), model, sampling (§10.1).
- [ ] Verify per-node auditability: from a `Fact`, reach its spans, source text, and
      producing `Action` (§10.2).

## Exit criteria

- [ ] Ingested propositions become a deduplicated graph of facts/actors/objects in the
      correct box and tier.
- [ ] No node or edge exists without provenance and an `Action` record.
- [ ] Reference vs case knowledge can be loaded into distinct boxes and queried both
      separately (by box) and jointly (by tier).
- [ ] Facts attach to a `PART_OF` hierarchy — anchored to a domain pack where coverage
      allows, induced+flagged otherwise — and a node's level resolves from its referent.

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
