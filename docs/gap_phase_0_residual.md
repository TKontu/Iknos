# Gap Plan — Phase 0 Residual (post-merge review)

**Why this file exists.** After Phase 0 was declared complete (all G0.* items in
`gap_phase_0_foundations.md` checked, PR #15 merged), a pipeline-level review of
the merged implementation looked for residual defects that would punish later
development. Each candidate was checked against the actual code to confirm it
**realizes** on a supported path rather than being theoretical. This file records
the **one confirmed issue** plus the candidates that were examined and dismissed
(so they are not re-raised).

**Refs:** §7.4 (bitemporal), §9 (boxes/tiers), §14 (part-whole). G0.7 (domain packs).

## Confirmed — fixed

### G0.R1 — `load_pack` rewrote `valid_from` on every reload *(idempotency / bitemporal integrity)* — **DONE**

**Resolution (implemented).** Packs are now **immutable per version** by
construction, so the full-replace `SET` never runs on a reload:

- `DomainPack.content_hash` (pure, in `pack.py`) — a canonical SHA-256 over the
  pack's *content* (identity `name`/`version` excluded; collections sorted), so
  reformatting/reordering the JSON does not trip the guard, only real change does.
- `load_pack` (`loader.py`) reads the existing Box's `content_hash` and branches:
  first load stamps `valid_from` + `content_hash` once; identical reload is a
  **true no-op** (no writes → `valid_from` preserved); changed content under the
  same version raises **`PackImmutabilityError`**; a legacy Box with no stored
  hash adopts it without touching `valid_from`. `valid_from` is now create-only.
- Tests: unit hash tests (stable / order-independent / change-sensitive /
  identity-independent) in `test_domain_pack.py`; integration tests in
  `test_domain_pack_load.py` now assert `valid_from` (Box **and** edge) is
  unchanged across reload — the assertion whose absence hid the bug — plus
  `content_hash` stamping and the immutability rejection. Full suite: 80 passed,
  ruff + mypy clean.

<details><summary>Original finding (for the record)</summary>

#### G0.R1 — `load_pack` rewrites `valid_from` on every reload *(idempotency / bitemporal integrity)*

**Symptom.** Re-loading an already-loaded domain pack (same `name`+`version`)
silently moves the bitemporal anchor: `Box.valid_from` and the `valid_from` on
**every** `directPartOf`/`partOf` edge are overwritten with a fresh "now". The
pack's content is unchanged, but its temporal provenance drifts on each reload.

**Why it realizes.** This is an explicitly supported, tested path, not a
hypothetical:
- `loader.py::load_pack` computes `stamp = valid_from or datetime.now(UTC)` —
  a fresh timestamp per call when no `valid_from` is passed (the normal case).
- `_merge_node` issues `MERGE (n:Label {id}) SET n = {body}` and `_merge_edge`
  issues `MERGE (a)-[r]->(b) SET r = {body}` — openCypher `SET x = {...}` is
  **full-replace**, not `+=` merge, so `valid_from` in `body` overwrites the
  stored value.
- `load_pack` computes `already = await is_pack_loaded(...)` but **never uses it
  for control flow** — the rewrite happens regardless. The flag is returned in
  `LoadedPack.already_loaded` and otherwise dead.
- The loader docstring promises the opposite: "re-activation, retries, and a
  re-run migration are all safe", "ingested once, read-only".

**Why the test misses it.** `tests/integration/test_domain_pack_load.py::
test_reload_is_idempotent` loads twice and asserts **counts** (no duplicate
vertices/edges) — which holds — but never asserts that properties (`valid_from`
in particular) are **stable** across the two loads. The corruption happens inside
this very test; it is just not observed.

**Fix (recommended).** Make reload a true no-op by using the flag already
computed. Domain packs are immutable per `(name, version)` (a new version is a
new Box, `pack.py::box_id`), and `load_pack` is atomic in the caller's
transaction (no partial committed state is possible — `is_pack_loaded` returning
true implies a prior *committed*, therefore *complete*, load). So:

- [ ] In `load_pack`, when `already` is true and no explicit `valid_from` is
      passed, **return early** with `LoadedPack(..., already_loaded=True)` without
      re-issuing writes. Preserves the original `valid_from`; matches the
      "ingested once, read-only" contract; relies on the existing transactional
      atomicity for completeness.
- [ ] Alternative if reloads must *repair* drifted props: split the upsert into
      `MERGE ... ON CREATE SET valid_from = $when, <rest> ON MATCH SET <rest>`
      (everything **except** `valid_from`), for both `_merge_node` (Box) and
      `_merge_edge`. More code; only needed if same-version repair is a goal.
- [ ] Regression test: extend `test_reload_is_idempotent` to capture
      `Box.valid_from` (and one edge's `valid_from`) after the first load and
      assert it is **unchanged** after the second. This is the assertion whose
      absence hid the bug.

**Blast radius if left.** `valid_from` has no reader yet (Phase 5 supersession is
the consumer), so nothing breaks *today* — but it is an audit/bitemporal field
being silently falsified on a "safe" operation, and the fix is one branch. Fix
before Phase 5 builds on the temporal record.

> **Note on the final implementation.** The shipped fix went one step beyond the
> "return early" option above: rather than only *preserving* `valid_from` on
> reload, it makes packs immutable per version and *rejects* changed-content
> reloads (`PackImmutabilityError`) instead of silently keeping stale graph data.
> This closes the dev/prod footgun where editing a pack without bumping its
> version would otherwise diverge from the declaration. See the resolution above.

</details>

## Reviewed and dismissed (not real / theoretical — do not re-raise)

These were considered in the same review and confirmed **not** to realize on any
current path. Recorded so the analysis is not repeated.

- **`SensitivityLevel` `<` is alphabetical, not lattice order.** Already handled:
  no production code compares levels (ordering goes through `_SENSITIVITY_RANK`
  and `Sensitivity.lub`), and `test_governance.py` pins the alphabetical `sorted`
  behavior as a deliberate characterization test. Documented decision, not a trap.
- **Cypher injection via raw f-string interpolation in `loader.py`.** Every
  raw-interpolated value is a UUID, ISO-8601 timestamp, or the `PACK_KIND`
  constant; all free-text (`name`, `source`, `label`, `description`, …) flows
  through `cypher_map`, which single-quote/backslash-escapes. No untrusted free
  text reaches a raw slot.
- **`_merge_edge` silently no-ops if an endpoint id is missing.** Its only caller
  (`load_pack`) MERGEs both endpoint `Object`s earlier in the same transaction, so
  the MATCH always resolves. Cannot trigger as used.
- **`entity_types` persisted as a JSON-encoded string property.** Round-trips
  correctly (the data contains no escape-sensitive characters); no requirement
  makes it Cypher-native, and the Phase 1 consumer parses the property. Works.
- **Part-whole edges carry only `valid_from`, not the full `BitemporalFields`
  quad.** The schema contract (`gap_phase_0_foundations.md` G0.3) requires
  bitemporal treatment only on `REFERS_TO`/`SAME_AS`, not part-whole. The lone
  `valid_from` is an ad-hoc stamp; its only real defect is the rewrite (G0.R1).
- **`HypothesisState` (3-way) vs `AcceptabilityBand` (4-way) have no mapping.**
  Both are vocabularies; no code computes either yet (Phase 4/6). Consistency is a
  design choice for the consumer, not a present defect. Worth a one-line note when
  Phase 4 wires the QBAF, not a Phase 0 fix.
- **`cypher_map` float formatting could emit sci-notation / `inf` / `nan`.** The
  only floats persisted today are `reliability_prior ∈ [0, 1]`. Revisit when
  arbitrary `strength`/`significance` scores get persisted (Phase 4).
- **Loading a new pack version does not deprecate the old one.** Intended:
  per-investigation activation (Phase 6) selects versions; `deprecate_pack` is the
  manual lever. Not a defect.
