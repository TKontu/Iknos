# C3 — AGE storage-engine viability benchmark (Trial C3)

> **Correction note (2026-06-12, post-review).** This is the *pre-fix* run; two items below are corrected after a review of the harness (`scripts/c3_age_density_benchmark.py`), and the report will be regenerated on the next live run:
> - **Shape 6 (supersession update) median is contaminated — do not quote 1.3 s.** The harness re-ran the UPDATE against the same ~2,500 edges across every rep in one uncommitted transaction (two `EXPLAIN ANALYZE` passes + warmup + 7 timed reps), so dead tuples accumulated and the ~1.3 s median / ~2.1 s p95 were **inflated and rep-order-dependent**. The harness now isolates each write rep (per-rep rollback to the committed baseline); the corrected figure **awaits a re-run** and is expected to be lower. The STAY decision is unaffected — the defect inflated an already-unindexed, deferred-by-design cost.
> - **`partOf` endpoint-btree use was not demonstrated.** The shape-3 EXPLAIN evidence shows only the **Actor GIN** (`ix_actor_props`); AGE's variable-length traversal may materialize edges internally and never emit a `partOf`-table index scan. Read shape 3 as *anchor Actor GIN verified; endpoint-btree use not demonstrated*. The ~60 ms closure latency stands; the index-attribution claim is withdrawn.
> - **`p95 ms` is the max at reps=7** (rank ⌊0.95·7⌋ = 6 of 7), not a true percentile — read it as worst-of-7.

- engine: single-engine **Postgres + Apache AGE**; bench graph: `iknos_c3_bench` (isolated, indexes mirror migration `0007`, dropped on teardown)
- scale: **30000** vertices across **40** boxes; **48993** edges; full 18-property vertex payload
- bitemporal anchor (as-of): `2024-01-01T00:00:00`; reps: 7 (median/p95, 1 warmup discarded)

## Decision

✅ **STAY single-engine (Postgres + AGE).** Every core read shape (box-scoped retrieval, partonomy closure, bitemporal as-of) has a *usable* migration-0007 index path verified through the real `cypher()` seam (existence ≠ use confirmed). MERGE-by-id resolves on the vertex GIN (index chosen). The only gap is the by-design deferred edge-property GIN (W9), which is Phase-5-scoped. No evidence for the separate-graph-store fallback at this density.

**Notes:**
- shape 3 variable-length closure is the costliest indexed read (60 ms median) — its **anchor** Actor GIN is chosen, so this is AGE variable-length-traversal overhead, not a missing index. Endpoint `partOf`-btree use is **not demonstrated** (see correction note). The synthetic partonomy is a near-linear chain (fan-out ≈1), so this closure is a depth-bounded linear walk, not a branching roll-up; a higher fan-out would cost more. Revisit if the partonomy roll-up becomes a hot path or its depth/fan-out grows.
- shapes 5, 6 (edge-property filter / supersession update): no accelerating index — the edge-property GIN is deferred to its consumer per the 0007 docstring. The supersession update is **the contaminated shape-6 measurement** (see correction note): the harness piled dead tuples across reps, so the recorded ~1.3 s median / ~2.1 s p95 is inflated and **pending re-measurement** with the now-isolated write reps. Directionally it is still ~10²–10³× the indexed lookups (it rewrites every matching edge with a seq scan over `SAME_AS`) — expected, not a regression. Phase 5 must add an edge-property GIN on `SAME_AS.properties` (or a btree on the extracted `state`) before bitemporal supersession runs at reference-base scale. Until then these are bounded by the small SAME_AS edge count, and real re-scoring touches a few edges at a time, not the bulk set this benchmark updates.

## Per-shape latency + index use

`index chosen` = planner picked the index unprompted; `index usable` = index is reachable for AGE's generated predicate at all (appears with `enable_seqscan=off`). The existence-vs-use distinction the trial demands.

| # | query shape | median ms | p95 ms | index chosen | index usable | verdict | note |
|---|-------------|----------:|-------:|:------------:|:------------:|---------|------|
| 1 box-scoped retrieval | `MATCH (n:Fact {box: 'box-000'}) RETURN count(n)` | 1.55 | 1.96 | yes | yes | ✅ index chosen | vertex GIN (`properties @>`) |
| 2 MERGE-by-id (resolution rate) | `MERGE (n:Actor {id: 'a-0000002'}) SET n.confiden…` | 0.64 | 0.73 | yes | yes | ✅ index chosen | vertex GIN — entity-resolution MERGE |
| 3 variable-length closure | `MATCH (a:Actor {id: 'a-0000008'})-[:partOf*1..5]…` | 59.54 | 65.36 | yes | yes | ✅ index chosen | anchor Actor GIN verified; endpoint partOf-btree use NOT demonstrated; partonomy fan-out ≈1 |
| 4 bitemporal as-of | `MATCH (n:Fact {box: 'box-000'}) WHERE n.valid_fr…` | 0.94 | 1.01 | yes | yes | ✅ index chosen | vertex GIN box prefilter + range scan |
| 5 edge-property filter (W9) | `MATCH ()-[r:SAME_AS {state: 'confirmed'}]->() RE…` | 19.68 | 25.34 | no | no | 🔸 no index (deferred) | edge-property GIN deferred — NO accelerating index (endpoint btree ≠ state filter) |
| 6 supersession update (W9) | `MATCH ()-[r:SAME_AS {state: 'candidate'}]->() SE…` | ⚠ 1332.86 (contaminated) | ⚠ 2145.89 | no | no | 🔸 no index (deferred) | re-scoring-rate edge update — **contaminated median, pending re-run** (see correction note); same deferred edge-property filter as shape 5 |

## EXPLAIN evidence (scan node touching the label table)

**1 box-scoped retrieval** — `MATCH (n:Fact {box: 'box-000'}) RETURN count(n)`
- default plan:        `->  Bitmap Heap Scan on "Fact" n  (cost=21.55..25.56 rows=1 width=578) (actual rows=250 loops=1)`
- enable_seqscan=off:  `->  Bitmap Heap Scan on "Fact" n  (cost=21.55..25.56 rows=1 width=578) (actual rows=250 loops=1)`

**2 MERGE-by-id (resolution rate)** — `MERGE (n:Actor {id: 'a-0000002'}) SET n.confidence = 0.5 RETURN n.id`
- default plan:        `->  Bitmap Heap Scan on "Actor" n  (cost=21.55..25.57 rows=1 width=64) (actual rows=1 loops=1)`
- enable_seqscan=off:  `->  Bitmap Heap Scan on "Actor" n  (cost=25.80..29.82 rows=1 width=64) (actual rows=1 loops=1)`

**3 variable-length closure** — `MATCH (a:Actor {id: 'a-0000008'})-[:partOf*1..5]->(b) RETURN count(b)`
- default plan:        `->  Bitmap Index Scan on ix_actor_props  (cost=0.00..30.04 rows=1 width=0) (actual rows=1 loops=1)`
- enable_seqscan=off:  `->  Bitmap Index Scan on ix_actor_props  (cost=0.00..30.04 rows=1 width=0) (actual rows=1 loops=1)`

**4 bitemporal as-of** — `MATCH (n:Fact {box: 'box-000'}) WHERE n.valid_from <= '2024-01-01T00:00:00' RETURN count(n)`
- default plan:        `->  Bitmap Heap Scan on "Fact" n  (cost=21.55..25.56 rows=1 width=578) (actual rows=250 loops=1)`
- enable_seqscan=off:  `->  Bitmap Heap Scan on "Fact" n  (cost=21.55..25.56 rows=1 width=578) (actual rows=250 loops=1)`

**5 edge-property filter (W9)** — `MATCH ()-[r:SAME_AS {state: 'confirmed'}]->() RETURN count(r)`
- default plan:        `->  Seq Scan on "SAME_AS" r  (cost=0.00..201.50 rows=2536 width=129) (actual rows=2500 loops=1)`
- enable_seqscan=off:  `->  Index Scan using ix_same_as_end on "SAME_AS" r  (cost=0.28..293.78 rows=2536 width=129) (actual rows=2500 loops=1)`

**6 supersession update (W9)** — `MATCH ()-[r:SAME_AS {state: 'candidate'}]->() SET r.valid_to = '2024-01-01T00:00:00' RETURN count(r)`
- default plan:        `->  Seq Scan on "SAME_AS" r  (cost=0.00..201.50 rows=2464 width=129) (actual rows=2500 loops=1)`
- enable_seqscan=off:  `->  Index Scan using ix_same_as_end on "SAME_AS" r  (cost=0.28..385.63 rows=2854 width=129) (actual rows=2500 loops=1)`

_(generation + ANALYZE: 154.4s for 30000 vertices.)_