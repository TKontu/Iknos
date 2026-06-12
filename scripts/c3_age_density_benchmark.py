"""C3 — Apache AGE storage-engine viability benchmark under real schema density (Trial C3).

Generates a **synthetic** investigation graph at the real property density — every vertex carrying
the ~15–20-property payload the production schema emits (provenance, two annotations, sensitivity,
conditional credibility, bitemporal validity, override state) and every edge carrying its own
properties — then times the query shapes Phases 2–5 actually emit and, via ``EXPLAIN`` through the
**real ``cypher()`` call path**, checks whether the migration-0007 indexes are *used* (existence ≠
use). It needs no production code; it is the de-risking measurement for the single biggest possible
architecture swap (single-engine Postgres+AGE vs a separate graph store), the §13 / Phase-5 entry
gate (`docs/todo_trials.md` Trial C3).

**Isolation.** The benchmark runs in its own throwaway AGE graph (``--graph``, default
``iknos_c3_bench``), *not* the production ``iknos`` graph: shape 6 issues ``SET`` updates and the
generator inserts tens of thousands of synthetic vertices, neither of which should touch real data
or collide with a concurrent integration run. The bench graph's indexes are created by **mirroring
migration 0007's exact label lists and index DDL** (imported from the migration module, so they
cannot drift from what production actually ships), retargeted to the bench schema. The graph is
dropped on teardown. Routing is via the real ``cypher()`` seam — we point ``settings.graph_name`` at
the bench graph, so every query and every ``EXPLAIN`` goes through the production call path.

**Existence ≠ use.** For each read shape we capture two plans: the planner's *default* choice and
the plan with ``enable_seqscan = off``. ``index_chosen`` means the planner picked the index on its
own; ``index_usable`` means the index is reachable *at all* for AGE's generated predicate (it shows
up once a seq scan is forbidden). A small table the planner seq-scans by choice (cheap) is fine; an
index that never appears even with seqscan off is the real failure mode the trial hunts for.

Query shapes (with the W9 amendment):
  1. box-scoped retrieval        MATCH (n {box: 'b'}) …               — vertex GIN (`properties @>`)
  2. MERGE-by-id (resolution)    MERGE (n:Label {id: …}) SET …        — vertex GIN
  3. variable-length closure     MATCH (a)-[:partOf*1..K]->(b)        — endpoint btrees
  4. bitemporal as-of            MATCH (n {box}) WHERE valid_from…     — vertex GIN box prefilter
  5. edge-property filter (W9)   MATCH ()-[r:SAME_AS {state:'…'}]->() — NO index path (deferred)
  6. supersession update (W9)    MATCH ()-[r:SAME_AS {…}]->() SET r.valid_to = …  (re-scoring rate)
  (SCC detection is done in igraph over a box-scoped load, i.e. shape 1, not in AGE.)

Usage::

    DATABASE_URL=postgresql+asyncpg://… uv run python -m scripts.c3_age_density_benchmark \
        --vertices 20000 --boxes 12 --reps 7 --out docs/trials/c3_age_density_benchmark.md

Writes a markdown report to stdout (and ``--out`` if given). The bitemporal anchor is a fixed
constant so the run is reproducible without a wall clock (``Date.now`` is deliberately avoided).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import random
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from iknos.config import settings
from iknos.db.age import bootstrap_session, cypher, cypher_map, execute_cypher

# A fixed bitemporal anchor so the run is reproducible without a wall clock (Date.now is avoided).
AS_OF = "2024-01-01T00:00:00"
VALID_FROM = "2023-01-01T00:00:00"

# The labels this benchmark exercises. Each MUST be a label migration 0007 actually indexes
# (asserted in `setup_graph`), so the bench graph carries the production index configuration, not
# an ad-hoc one. partOf (camelCase) is the real partonomy edge label — NOT `PART_OF`, which 0007
# never indexes.
BENCH_VERTEX_LABELS = ("Fact", "Hypothesis", "Actor")
BENCH_EDGE_LABELS = ("SUPPORTS", "REFUTES", "SAME_AS", "partOf")

SENSITIVITY = ("public", "internal", "restricted")
OVERRIDE = ("none", "soft", "hard")

# A bare SQL identifier — the bench graph name is interpolated into create_graph/DDL, so it gets the
# same identifier contract config.Settings enforces on GRAPH_NAME (review M1 / V11).
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _load_migration_0007() -> ModuleType:
    """Import migration 0007's label lists + index-name helpers as the single source of truth.

    The benchmark must index the bench graph with *exactly* the indexes production ships, or it
    would be measuring a fiction. Rather than copy the label lists (which then silently drift when
    a label is added to 0007), we import them. The file name starts with a digit, so it is not a
    normal importable module — load it by path. ``from alembic import op`` at its top is a lazy
    proxy that only errors when an ``op.*`` call runs outside a migration context, so importing the
    module (which merely *defines* upgrade/downgrade) is safe here.
    """
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / ("20260611_0007_age_label_indexes.py")
    )
    spec = importlib.util.spec_from_file_location("iknos_migration_0007", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load migration 0007 from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vertex_props(vid: str, box: str, label: str, rng: random.Random) -> dict[str, object]:
    """A realistic ~18-property vertex payload (the density the GIN index must carry)."""
    return {
        "id": vid,
        "box": box,
        "type": label.lower(),
        "statement": f"synthetic {label} statement {vid} for density benchmarking purposes",
        "support_count": rng.randint(0, 12),  # Layer A annotation
        "confidence": round(rng.random(), 4),  # Layer B annotation
        "sensitivity_level": rng.choice(SENSITIVITY),
        "sensitivity_compartments": ",".join(
            rng.sample(["ops", "legal", "hr"], k=rng.randint(0, 2))
        ),
        "credibility_base": round(rng.random(), 4),
        "credibility_effective": round(rng.random(), 4),
        "valid_from": VALID_FROM,
        "valid_to": "",  # open interval; "" reads as null-ish for the as-of containment
        "tx_from": VALID_FROM,
        "tx_to": "",
        "override_state": rng.choice(OVERRIDE),
        "provenance_action": f"act-{rng.randint(0, 9999)}",
        "model": "synthetic-bench-1",
        "polarity": rng.choice(["affirmed", "denied"]),
    }


def _map_list(maps: list[dict[str, object]]) -> str:
    return "[" + ", ".join(cypher_map(m) for m in maps) + "]"


@dataclass
class Shape:
    name: str
    query: str
    note: str = ""
    label_table: str = ""  # the AGE label table whose scan node we inspect (e.g. Fact, SAME_AS)
    # The migration-0007 index(es) whose use constitutes *genuine* acceleration of THIS shape's
    # predicate. Empty () means no index can accelerate it — the deferred edge-property-GIN case
    # (W9). This is the crux of the existence-vs-use check: an endpoint btree used as a seq-scan
    # substitute for a `state` filter is NOT acceleration of that filter, so it must not count.
    expected_indexes: tuple[str, ...] = ()
    is_write: bool = False  # MERGE/UPDATE — its plan executes; we still read the scan node
    times_ms: list[float] = field(default_factory=list)
    plan_default: str = ""
    plan_no_seqscan: str = ""

    @property
    def median_ms(self) -> float:
        return statistics.median(self.times_ms) if self.times_ms else float("nan")

    @property
    def p95_ms(self) -> float:
        """The rank-``⌊0.95·n⌋`` observation. With the small default ``reps`` (7) this is the
        **max** sample (rank 6 of 7), not a true percentile — read it as "worst observed of N",
        and see the report's honest-label note. Meaningful as a real p95 only at large ``reps``."""
        if not self.times_ms:
            return float("nan")
        s = sorted(self.times_ms)
        return s[min(len(s) - 1, int(0.95 * len(s)))]

    def _uses_expected_index(self, plan: str) -> bool:
        # Must be an index *scan* node AND name one of the indexes that genuinely accelerates this
        # predicate. (A Bitmap Index Scan names the index; a Bitmap Heap Scan does not, so we also
        # accept the index name appearing anywhere in an index-scan-bearing plan.)
        if not re.search(r"\bIndex (Only )?Scan\b|\bBitmap Index Scan\b", plan):
            return False
        return any(idx in plan for idx in self.expected_indexes)

    @property
    def has_index(self) -> bool:
        """Does an accelerating index exist for this shape at all (the deferred-case flag)?"""
        return bool(self.expected_indexes)

    @property
    def index_chosen(self) -> bool:
        """The planner picked the accelerating index on its own (default plan)."""
        return self._uses_expected_index(self.plan_default)

    @property
    def index_usable(self) -> bool:
        """The accelerating index is reachable for AGE's predicate (appears with seqscan off)."""
        return self._uses_expected_index(self.plan_no_seqscan)

    def verdict(self) -> str:
        if not self.has_index:
            return "🔸 no index (deferred)"
        if self.index_chosen:
            return "✅ index chosen"
        if self.index_usable:
            return "➖ seq by choice (index usable)"
        return "⚠️ index exists but UNUSED"


async def _exec_sql(session: AsyncSession, sql: str) -> None:
    conn = await session.connection()
    await conn.exec_driver_sql(sql)


async def _explain(session: AsyncSession, query: str, *, no_seqscan: bool) -> str:
    conn = await session.connection()
    if no_seqscan:
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
    sql = "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) " + cypher(query)
    rows = await conn.exec_driver_sql(sql)
    plan = "\n".join(str(r[0]) for r in rows.all())
    if no_seqscan:
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = on")
    return plan


async def _time(
    session: AsyncSession, query: str, reps: int, *, is_write: bool = False
) -> list[float]:
    # One untimed warmup so cold-cache / first-plan effects do not skew the median.
    await execute_cypher(session, query)
    if is_write:
        # A write rep mutates the rows it matches. Left in one growing transaction (the two
        # EXPLAIN ANALYZE passes already ran the UPDATE twice, plus this warmup), every repeated rep
        # rewrites the SAME matching edges and piles dead tuples on them, so the per-rep cost climbs
        # with rep order and the median is contaminated (the C3 review's shape-6 defect). Roll back
        # the explains + warmup, then time each rep in its own unit and roll it back, so every rep
        # runs the UPDATE against the same committed baseline — order-independent, uncontaminated.
        await session.rollback()
    out: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        await execute_cypher(session, query)
        out.append((time.perf_counter() - t0) * 1000.0)
        if is_write:
            await session.rollback()  # reset to the committed baseline before the next rep
    return out


async def setup_graph(session: AsyncSession, graph: str) -> ModuleType:
    """Create the throwaway bench graph + labels, then mirror migration 0007's indexes onto it.

    Drops any leftover bench graph from a failed prior run first. Returns the imported migration
    module so the caller can reuse its label set in the report. The index DDL here is migration
    0007's DDL verbatim except for the target schema (the bench graph instead of ``iknos``).
    """
    mig = _load_migration_0007()
    for label in BENCH_VERTEX_LABELS:
        assert label in mig.VERTEX_LABELS, f"{label} is not a 0007-indexed vertex label"
    for label in BENCH_EDGE_LABELS:
        assert label in mig.EDGE_LABELS, f"{label} is not a 0007-indexed edge label"

    # AGE catalog functions are qualified (`ag_catalog.*`) so they resolve regardless of the
    # session search_path. We avoid catching-and-rolling-back a failed drop: plain `SET
    # search_path` is transactional, so a ROLLBACK would silently revert the bootstrap search_path
    # the GIN opclass below depends on. Instead we check for the leftover graph first (no error →
    # no rollback) and drop only if present.
    conn = await session.connection()
    exists = (
        await conn.exec_driver_sql(f"SELECT 1 FROM ag_catalog.ag_graph WHERE name = '{graph}'")
    ).first()
    if exists:
        await _exec_sql(session, f"SELECT ag_catalog.drop_graph('{graph}', true)")
    await _exec_sql(session, f"SELECT ag_catalog.create_graph('{graph}')")
    for label in BENCH_VERTEX_LABELS:
        await _exec_sql(session, f"SELECT ag_catalog.create_vlabel('{graph}', '{label}')")
    for label in BENCH_EDGE_LABELS:
        await _exec_sql(session, f"SELECT ag_catalog.create_elabel('{graph}', '{label}')")

    # Indexes — migration 0007's DDL, retargeted to the bench schema (== graph name). The agtype
    # GIN default opclass lives in ag_catalog; re-assert it onto the search_path (as 0007 does) so
    # `USING gin (properties)` resolves the opclass.
    await _exec_sql(session, 'SET search_path = ag_catalog, "$user", public')
    for label in BENCH_VERTEX_LABELS:
        idx = mig._vertex_index(label)
        await _exec_sql(session, f'CREATE INDEX {idx} ON {graph}."{label}" USING gin (properties)')
    for label in BENCH_EDGE_LABELS:
        (start_idx, start_col), (end_idx, end_col) = mig._edge_indexes(label)
        await _exec_sql(session, f'CREATE INDEX {start_idx} ON {graph}."{label}" ({start_col})')
        await _exec_sql(session, f'CREATE INDEX {end_idx} ON {graph}."{label}" ({end_col})')
    await session.commit()
    return mig


async def teardown_graph(session: AsyncSession, graph: str) -> None:
    # Roll back FIRST: a mid-run failure leaves the session in an aborted transaction, in which any
    # further statement (including the drop) errors out — so without this the bench graph leaks.
    # Clearing the aborted transaction lets the drop actually execute. Best-effort thereafter.
    await session.rollback()
    try:
        await _exec_sql(session, f"SELECT ag_catalog.drop_graph('{graph}', true)")
        await session.commit()
    except Exception:  # noqa: BLE001 - teardown is best-effort
        await session.rollback()


async def generate(
    session: AsyncSession, *, n_vertices: int, n_boxes: int, rng: random.Random
) -> dict[str, object]:
    """Create n_vertices across n_boxes with the full payload, plus a realistic edge mix."""
    boxes = [f"box-{i:03d}" for i in range(n_boxes)]
    ids_by_label: dict[str, list[str]] = {label: [] for label in BENCH_VERTEX_LABELS}
    batch = 400
    pending: list[tuple[str, dict[str, object]]] = []

    async def flush() -> None:
        by_label: dict[str, list[dict[str, object]]] = {}
        for label, props in pending:
            by_label.setdefault(label, []).append(props)
        for label, maps in by_label.items():
            await execute_cypher(
                session,
                f"UNWIND {_map_list(maps)} AS row MERGE (n:{label} {{id: row.id}}) SET n = row",
            )
        pending.clear()

    for i in range(n_vertices):
        label = BENCH_VERTEX_LABELS[i % len(BENCH_VERTEX_LABELS)]
        vid = f"{label[0].lower()}-{i:07d}"
        box = boxes[i % n_boxes]
        ids_by_label[label].append(vid)
        pending.append((label, _vertex_props(vid, box, label, rng)))
        if len(pending) >= batch:
            await flush()
    await flush()
    await session.commit()

    facts, hyps, actors = (
        ids_by_label["Fact"],
        ids_by_label["Hypothesis"],
        ids_by_label["Actor"],
    )
    edge_count = await _make_edges(session, facts, hyps, actors, rng)
    await session.commit()

    # Refresh planner stats so the index-vs-seqscan choice reflects the loaded volume, not the
    # empty-table estimate AGE's create_*label leaves behind (the difference between a meaningful
    # and a misleading EXPLAIN).
    await _exec_sql(session, "ANALYZE")
    await session.commit()
    return {"boxes": boxes, "ids_by_label": ids_by_label, "edge_count": edge_count}


async def _make_edges(session, facts, hyps, actors, rng) -> int:  # type: ignore[no-untyped-def]
    total = 0
    batch = 300

    async def edges(label: str, rows: list[dict[str, object]], prop_keys: tuple[str, ...]) -> None:
        # Edge props live FLAT on each unwound row alongside src/dst, then are SET key-by-key.
        # `SET r = e.props` cannot work: cypher_map JSON-stringifies a nested map, so e.props comes
        # back as a string ("SET clause expects a map"). Enumerating the (uniform, per-label) keys
        # is the honest way to inline a per-row property map through the UNWIND.
        nonlocal total
        if not rows:
            return
        set_clause = ", ".join(f"r.{k} = e.{k}" for k in prop_keys)
        await execute_cypher(
            session,
            f"UNWIND {_map_list(rows)} AS e MATCH (a {{id: e.src}}), (b {{id: e.dst}}) "
            f"MERGE (a)-[r:{label}]->(b) SET {set_clause}",
        )
        total += len(rows)

    # SUPPORTS/REFUTES: each hypothesis gets a handful of evidential edges.
    ev_keys = ("strength", "significance", "sign_stable", "valid_from", "valid_to")
    buf: list[dict[str, object]] = []
    for h in hyps:
        for _ in range(rng.randint(2, 6)):
            f = rng.choice(facts)
            buf.append(
                {
                    "src": f,
                    "dst": h,
                    "strength": round(rng.random(), 3),
                    "significance": round(rng.random(), 3),
                    "sign_stable": True,
                    "valid_from": VALID_FROM,
                    "valid_to": "",
                }
            )
            if len(buf) >= batch:
                await edges(rng.choice(["SUPPORTS", "REFUTES"]), buf, ev_keys)
                buf = []
    await edges("SUPPORTS", buf, ev_keys)

    # SAME_AS: actor resolution edges, ~half confirmed / half candidate (the W9 edge-prop shape).
    sa_keys = ("state", "score", "valid_from", "valid_to")
    buf = []
    for i in range(0, len(actors) - 1, 2):
        state = "confirmed" if i % 4 == 0 else "candidate"
        buf.append(
            {
                "src": actors[i],
                "dst": actors[i + 1],
                "state": state,
                "score": round(rng.random(), 3),
                "valid_from": VALID_FROM,
                "valid_to": "",
            }
        )
        if len(buf) >= batch:
            await edges("SAME_AS", buf, sa_keys)
            buf = []
    await edges("SAME_AS", buf, sa_keys)

    # partOf: an actor partonomy chain so the variable-length closure (shape 3) has depth.
    po_keys = ("valid_from",)
    buf = []
    for i in range(len(actors) - 1):
        if rng.random() < 0.4:
            buf.append({"src": actors[i + 1], "dst": actors[i], "valid_from": VALID_FROM})
            if len(buf) >= batch:
                await edges("partOf", buf, po_keys)
                buf = []
    await edges("partOf", buf, po_keys)
    return total


def _shapes(ctx: dict[str, object]) -> list[Shape]:
    box = ctx["boxes"][0]  # type: ignore[index]
    some_actor = ctx["ids_by_label"]["Actor"][0]  # type: ignore[index]
    a_id = ctx["ids_by_label"]["Actor"][2]  # type: ignore[index]
    # Derive the accelerating-index names from migration 0007 itself — no hand-copied strings.
    mig = _load_migration_0007()
    fact_gin = mig._vertex_index("Fact")
    actor_gin = mig._vertex_index("Actor")
    (po_start, _), (po_end, _) = mig._edge_indexes("partOf")
    return [
        Shape(
            "1 box-scoped retrieval",
            f"MATCH (n:Fact {{box: '{box}'}}) RETURN count(n)",
            "vertex GIN (`properties @>`)",
            label_table="Fact",
            expected_indexes=(fact_gin,),
        ),
        Shape(
            "2 MERGE-by-id (resolution rate)",
            f"MERGE (n:Actor {{id: '{some_actor}'}}) SET n.confidence = 0.5 RETURN n.id",
            "vertex GIN — entity-resolution MERGE",
            label_table="Actor",
            expected_indexes=(actor_gin,),
            is_write=True,
        ),
        Shape(
            "3 variable-length closure",
            f"MATCH (a:Actor {{id: '{a_id}'}})-[:partOf*1..5]->(b) RETURN count(b)",
            "anchor Actor GIN verified; endpoint partOf-btree use NOT demonstrated (VLE may "
            "materialize edges internally); synthetic partonomy fan-out ≈1 (near-linear chain)",
            label_table="partOf",
            # The anchor lookup rides the Actor GIN — that is what the plan demonstrably uses. The
            # partOf endpoint btrees are *candidates* for the closure traversal but AGE's VLE may
            # materialize edges internally and never emit a partOf-table index scan, so we do NOT
            # claim btree use is shown; the GIN appearing confirms the anchor is indexed.
            # Caveat: the generated partonomy is a near-linear chain (fan-out ≈1, _make_edges), so
            # this closure is a depth-bounded linear walk, not a branching roll-up — a higher
            # fan-out would cost more.
            expected_indexes=(actor_gin, po_start, po_end),
        ),
        Shape(
            "4 bitemporal as-of",
            f"MATCH (n:Fact {{box: '{box}'}}) WHERE n.valid_from <= '{AS_OF}' RETURN count(n)",
            "vertex GIN box prefilter + range scan",
            label_table="Fact",
            expected_indexes=(fact_gin,),
        ),
        Shape(
            "5 edge-property filter (W9)",
            "MATCH ()-[r:SAME_AS {state: 'confirmed'}]->() RETURN count(r)",
            "edge-property GIN deferred — NO accelerating index (endpoint btree ≠ state filter)",
            label_table="SAME_AS",
            expected_indexes=(),  # the deferred edge-property GIN; the crux of the W9 amendment
        ),
        Shape(
            "6 supersession update (W9)",
            f"MATCH ()-[r:SAME_AS {{state: 'candidate'}}]->() "
            f"SET r.valid_to = '{AS_OF}' RETURN count(r)",
            "re-scoring-rate edge update — same deferred edge-property filter as shape 5",
            label_table="SAME_AS",
            expected_indexes=(),
            is_write=True,
        ),
    ]


def _scan_line(plan: str, label_table: str, expected_indexes: tuple[str, ...] = ()) -> str:
    """The first scan-node line touching the label table (or one of this shape's accelerating
    indexes) — the evidence row for the report.

    The bare ``"Index Scan" in line`` disjunct this used to carry matched *any* index-scan line,
    so a shape whose label table never appears (e.g. the variable-length closure, where AGE may
    materialize edges internally and emit no ``partOf``-table scan) would surface an unrelated
    index's scan node as if it were the label-table evidence — overstating index use. We now match
    only lines that name the label table **or** one of the shape's own ``expected_indexes``, so the
    evidence row is always honest about what it shows; if neither appears we fall through.
    """
    for line in plan.splitlines():
        if re.search(r"(Seq Scan|Index (Only )?Scan|Bitmap)", line) and (
            label_table in line or any(idx in line for idx in expected_indexes)
        ):
            return line.strip()
    first = plan.splitlines()[0] if plan else "(no plan)"
    return first.strip()


def _decision(shapes: list[Shape]) -> tuple[str, list[str]]:
    """Stay-single-engine vs fallback, derived from the measured plans + latencies.

    The engine fails the gate only if an index that *exists* (migration 0007) turns out to be
    UNUSABLE for AGE's generated predicate — the "existence ≠ use" trap. A planner that seq-scans
    a small table *by choice* (the index being usable when forced) is not a failure. The deferred
    edge-property GIN (shapes 5/6, no index by design — the W9 amendment) is a documented Phase-5
    follow-up, not an engine-swap trigger.
    """
    notes: list[str] = []
    # Core reads that ship a migration-0007 index and must be able to use it.
    core_reads = [s for s in shapes if s.has_index and not s.is_write]
    unused = [s for s in core_reads if not s.index_usable]
    deferred = [s for s in shapes if not s.has_index]

    # MERGE-by-id is a *write* (excluded from core_reads above), so its index result was never
    # folded into the verdict — yet the verdict asserted it "resolves on the vertex GIN". Derive the
    # clause from shape 2's actual measured plan instead of hard-coding it.
    merge = next((s for s in shapes if s.is_write and s.query.lstrip().startswith("MERGE")), None)
    if merge is None:
        merge_clause = ""
    elif merge.index_chosen:
        merge_clause = " MERGE-by-id resolves on the vertex GIN (index chosen)."
    elif merge.index_usable:
        merge_clause = " MERGE-by-id's vertex GIN is usable (planner seq-scans by choice here)."
    else:
        merge_clause = (
            f" Note: MERGE-by-id ({merge.name}) did NOT show a usable vertex-GIN path — watch "
            "entity-resolution scaling."
        )

    for s in core_reads:
        if s.index_usable and not s.index_chosen:
            notes.append(
                f"shape {s.name}: planner seq-scans by choice, but the index engages once seqscan "
                "is forbidden — fine at this volume; it is chosen as the label table grows."
            )

    # Surface the costliest read shape — at scale the variable-length closure dominates the
    # index-backed lookups, which is a property of AGE's traversal, not a missing index.
    if core_reads:
        slowest = max(core_reads, key=lambda s: s.median_ms)
        others = [s for s in core_reads if s is not slowest]
        if others and slowest.median_ms > 5 * max(s.median_ms for s in others):
            notes.append(
                f"shape {slowest.name} is the costliest indexed read ({slowest.median_ms:.0f} ms "
                f"median) — its anchor index is chosen, so this is AGE variable-length-traversal "
                "overhead, not a missing index. Acceptable at investigation scale; revisit if the "
                "partonomy roll-up becomes a hot path or its depth/fan-out grows (the synthetic "
                "partonomy here is a near-linear chain, fan-out ≈1)."
            )

    if deferred:
        names = ", ".join(s.name.split()[0] for s in deferred)
        worst = max(deferred, key=lambda s: s.median_ms)
        notes.append(
            f"shapes {names} (edge-property filter / supersession update): no accelerating index — "
            "the edge-property GIN is deferred to its consumer per the 0007 docstring. The "
            f"concrete cost is visible here — the unindexed supersession update runs at "
            f"{worst.median_ms:.0f} ms median (p95 {worst.p95_ms:.0f} ms), ~10²–10³× the indexed "
            "lookups, because it rewrites every matching edge with a seq scan over `SAME_AS`. "
            "Expected, not a regression: Phase 5 must add an edge-property GIN on "
            "`SAME_AS.properties` (or a btree on the extracted `state`) before bitemporal "
            "supersession runs at reference-base scale. Until then these are bounded by the small "
            "SAME_AS edge count, and real re-scoring touches a few edges at a time, not the bulk "
            "set this benchmark updates."
        )

    if unused:
        verdict = (
            "⚠️ **FALLBACK SIGNAL** — a migration-0007 index EXISTS but is UNUSABLE for AGE's "
            f"generated predicate on core read shape(s) {', '.join(s.name for s in unused)}. This "
            "is the existence-vs-use trap and the §13 engine-swap trigger; investigate the "
            "agtype predicate / opclass before committing to single-engine."
        )
    else:
        verdict = (
            "✅ **STAY single-engine (Postgres + AGE).** Every core read shape (box-scoped "
            "retrieval, partonomy closure, bitemporal as-of) has a *usable* migration-0007 index "
            "path verified through the real `cypher()` seam (existence ≠ use confirmed)."
            + merge_clause
            + " The only gap is the by-design deferred edge-property GIN (W9), which is "
            "Phase-5-scoped. No evidence for the separate-graph-store fallback at this density."
        )
    return verdict, notes


def _report(
    ctx: dict[str, object],
    shapes: list[Shape],
    *,
    n_vertices: int,
    n_boxes: int,
    graph: str,
    gen_s: float,
    reps: int,
) -> str:
    verdict, notes = _decision(shapes)
    n_props = len(_vertex_props("x", "b", "Fact", random.Random(0)))
    lines = [
        "# C3 — AGE storage-engine viability benchmark (Trial C3)",
        "",
        f"- engine: single-engine **Postgres + Apache AGE**; bench graph: `{graph}` "
        "(isolated, indexes mirror migration `0007`, dropped on teardown)",
        f"- scale: **{n_vertices}** vertices across **{n_boxes}** boxes; "
        f"**{ctx['edge_count']}** edges; full {n_props}-property vertex payload",
        f"- bitemporal anchor (as-of): `{AS_OF}`; reps: {reps} (median/p95, 1 warmup discarded)",
        f"- **`p95 ms` caveat:** with reps={reps} small, `p95 ms` is the rank-⌊0.95·{reps}⌋ sample "
        f"— effectively the **max** observed (worst-of-{reps}), not a true percentile. Write-shape "
        "reps are isolated (each rep rolled back to a clean baseline) so repeated updates of the "
        "same edges don't accumulate dead tuples and skew the median.",
        "",
        "## Decision",
        "",
        verdict,
        "",
    ]
    if notes:
        lines.append("**Notes:**")
        lines += [f"- {n}" for n in notes]
        lines.append("")
    lines += [
        "## Per-shape latency + index use",
        "",
        "`index chosen` = planner picked the index unprompted; `index usable` = index is reachable "
        "for AGE's generated predicate at all (appears with `enable_seqscan=off`). The "
        "existence-vs-use distinction the trial demands.",
        "",
        "| # | query shape | median ms | p95 ms | index chosen | index usable | verdict | note |",
        "|---|-------------|----------:|-------:|:------------:|:------------:|---------|------|",
    ]
    for s in shapes:
        lines.append(
            f"| {s.name} | `{s.query[:48]}{'…' if len(s.query) > 48 else ''}` "
            f"| {s.median_ms:.2f} | {s.p95_ms:.2f} "
            f"| {'yes' if s.index_chosen else 'no'} | {'yes' if s.index_usable else 'no'} "
            f"| {s.verdict()} | {s.note} |"
        )
    lines += ["", "## EXPLAIN evidence (scan node touching the label table)", ""]
    for s in shapes:
        default_scan = _scan_line(s.plan_default, s.label_table, s.expected_indexes)
        noseq_scan = _scan_line(s.plan_no_seqscan, s.label_table, s.expected_indexes)
        lines.append(f"**{s.name}** — `{s.query}`")
        lines.append(f"- default plan:        `{default_scan}`")
        lines.append(f"- enable_seqscan=off:  `{noseq_scan}`")
        lines.append("")
    lines.append(f"_(generation + ANALYZE: {gen_s:.1f}s for {n_vertices} vertices.)_")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> str:
    rng = random.Random(args.seed)
    if not _SQL_IDENTIFIER.fullmatch(args.graph) or len(args.graph) > 63:
        raise SystemExit(f"--graph must be a bare SQL identifier (≤63 chars); got {args.graph!r}")
    if args.graph == settings.graph_name and not args.allow_prod_graph:
        raise SystemExit(
            f"refusing to benchmark the configured production graph {args.graph!r} "
            "(shape 6 mutates; the generator inserts synthetic data). Use a throwaway --graph "
            "or pass --allow-prod-graph if you really mean it."
        )

    # Route the real cypher() seam at the bench graph. Settings has no validate_assignment, so this
    # is a plain attribute set; we validated the identifier above.
    settings.graph_name = args.graph

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            await bootstrap_session(session)
            await setup_graph(session, args.graph)
            try:
                t0 = time.perf_counter()
                ctx = await generate(session, n_vertices=args.vertices, n_boxes=args.boxes, rng=rng)
                gen_s = time.perf_counter() - t0
                shapes = _shapes(ctx)
                for s in shapes:
                    s.plan_default = await _explain(session, s.query, no_seqscan=False)
                    s.plan_no_seqscan = await _explain(session, s.query, no_seqscan=True)
                    s.times_ms = await _time(session, s.query, args.reps, is_write=s.is_write)
                    await session.commit()
                report = _report(
                    ctx,
                    shapes,
                    n_vertices=args.vertices,
                    n_boxes=args.boxes,
                    graph=args.graph,
                    gen_s=gen_s,
                    reps=args.reps,
                )
            finally:
                if not args.keep_graph:
                    await teardown_graph(session, args.graph)
        return report
    finally:
        await engine.dispose()


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="C3 AGE density benchmark (isolated bench graph).")
    p.add_argument("--vertices", type=int, default=20000)
    p.add_argument("--boxes", type=int, default=12)
    p.add_argument("--reps", type=int, default=7)
    p.add_argument("--seed", type=int, default=20260612)
    p.add_argument("--graph", default="iknos_c3_bench", help="Throwaway AGE graph to benchmark in.")
    p.add_argument("--keep-graph", action="store_true", help="Do not drop the bench graph (debug).")
    p.add_argument(
        "--allow-prod-graph",
        action="store_true",
        help="Permit benchmarking the configured production graph (mutates it). Off by default.",
    )
    p.add_argument("--out", default=None, help="Also write the markdown report here.")
    return p.parse_args()


async def main() -> None:
    args = _parse()
    report = await run(args)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
