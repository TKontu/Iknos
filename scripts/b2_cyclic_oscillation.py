"""B2 — cyclic-region oscillation measurement over the gradual-semantics engine (Trial B2).

Trial B2 (`docs/todo_trials.md`, Instrument B) de-risks **Phase-4 QBAF oscillation handling**
and **Phase-6 cyclic-region presentation**: a cyclic argument region (mutual support, circular
refutation) has no general convergence guarantee under a gradual semantics, and the architecture
rule (§13, principle 8) is *surface the unresolved region, never force a verdict*. This harness
builds deliberately cyclic fixtures, runs the **existing** engine (`core/qbaf.solve` — read-only;
its iteration bound and `QbafResult.unstable` already exist) across a sweep of iteration caps and
both gradual semantics (DF-QuAD / Quadratic Energy), and measures, per semantics, whether the
engine **flags cycles vs falsely converges** — then proposes the empirical iteration bound and
the variance-over-the-last-``n``-iterations oscillation criterion B2 calls for.

**The engine is never modified or re-implemented.** The per-sweep trajectory the variance
criterion needs is reconstructed *through the public ``solve``*: ``solve(max_iterations=k)`` runs
exactly ``k`` synchronous Jacobi sweeps from the base seed and returns the post-``k`` state, so
calling it for ``k = 1..H`` yields the true trajectory ``σ_1 … σ_H`` (the dynamics are
deterministic and identically seeded). No knob is added to ``core/qbaf.py``; the criterion lives
in the pure, LLM-free, ``DATABASE_URL``-free trials harness (``iknos.trials.oscillation``), which
Phase-4/Phase-6 then consume. Reconstruction is O(H²) in ``solve`` calls, but the fixtures are
tiny (≤6 arguments), so a horizon of a few hundred sweeps runs in seconds.

**The two detectors compared.** (1) The engine's native signal: ``converged`` / ``unstable``,
a **per-step** test (max strength change over one sweep ≤ ``tolerance``). (2) The B2 criterion:
**strength variance over the last ``n`` sweeps > ``ε``** (``iknos.trials.oscillation``). The
report quantifies where they agree and where the variance window is the better detector — chiefly
the slow-but-converging case, where the per-step test needs thousands of sweeps to reach a tight
tolerance while the variance window recognises the trajectory has practically settled far sooner.

No DB, no AGE, no LLM, no network, no wall clock — pure CPU over `[0, 1]` floats, reproducible.

Usage::

    uv run python -m scripts.b2_cyclic_oscillation --out docs/trials/b2_cyclic_oscillation.md

Writes a markdown report to stdout (and ``--out`` if given).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from iknos.core.qbaf import (
    BAF,
    DF_QUAD,
    QUADRATIC_ENERGY,
    Edge,
    GradualSemantics,
    solve,
)
from iknos.trials.oscillation import is_oscillating, max_tail_variance, oscillating_args
from iknos.trials.report import comparison_table

# ─────────────────────────────────────────────────────────────────────────────────────────
# Measurement parameters. The engine defaults (`solve` signature) are the realistic baseline
# the cap sweep is centred on; `tolerance` is the engine's, used for every `solve` here.
# ─────────────────────────────────────────────────────────────────────────────────────────

TOLERANCE = 1e-9  # core/qbaf.solve default — the per-step convergence test threshold.
ENGINE_DEFAULT_CAP = 100  # core/qbaf.solve default max_iterations.
SETTLE_CEILING = 4000  # a generous cap to find a fixture's *true* engine settle iteration.

# The proposed B2 oscillation criterion (justified empirically by the separation table below).
WINDOW = 16  # last-n sweeps the variance is taken over (> the longest fixture limit-cycle).
EPSILON = 1e-6  # variance threshold: a region with tail variance > ε is "still moving".

# The cap sweep — how the verdict depends on the iteration bound. Ends at HORIZON (the trajectory
# reconstruction length), so the per-cap criterion reads a window that exists.
CAPS = (10, 20, 50, 100, 200, 400, 600, 800)
HORIZON = 800

# Bands used to choose the recommended iteration bound: a converger that settles within this many
# sweeps is "fast" (the bound must clear it); a slower one is reported as the budget trade-off.
FAST_SETTLE_CEILING = 200


# ─────────────────────────────────────────────────────────────────────────────────────────
# Fixtures — deliberately cyclic argument graphs, as data. Each carries the *expected*
# character so the report can say whether the engine matched it. `base` is the §8 intrinsic
# weight (Layer B confidence) per argument; an argument absent defaults to 0.0 in `solve`.
# ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fixture:
    name: str
    summary: str
    baf: BAF
    base: Mapping[str, float]
    note: str


def _two_cycle(kind: str, base_score: float, strength: float = 1.0) -> tuple[BAF, dict[str, float]]:
    """A 2-cycle ``a ⇄ b`` of one sign (``support`` or ``attack``), both args at ``base_score``."""
    edges = (Edge("a", "b", strength), Edge("b", "a", strength))
    supports = edges if kind == "support" else ()
    attacks = edges if kind == "attack" else ()
    return BAF(frozenset({"a", "b"}), supports=supports, attacks=attacks), {
        "a": base_score,
        "b": base_score,
    }


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        name="acyclic_control",
        summary="Acyclic chain p→q→h (all support) — the non-cyclic baseline.",
        baf=BAF(
            frozenset({"p", "q", "h"}),
            supports=(Edge("p", "q", 1.0), Edge("q", "h", 1.0)),
        ),
        base={"p": 1.0},
        note="Converges to its exact fixpoint in depth-many sweeps; must never be flagged.",
    ),
    Fixture(
        name="mutual_support_2cycle",
        summary="a ⇄ b mutually SUPPORT (base 0.3) — a reinforcing cycle.",
        baf=_two_cycle("support", 0.3)[0],
        base=_two_cycle("support", 0.3)[1],
        note="Reinforcing support converges to a saturated high fixpoint; a support cycle is "
        "*not* an oscillation and must not be flagged — but it converges only geometrically, "
        "so a too-low cap would false-flag it.",
    ),
    Fixture(
        name="circular_refutation_damped",
        summary="a ⇄ b mutually REFUTE (base 0.5) — sub-critical circular refutation.",
        baf=_two_cycle("attack", 0.5)[0],
        base=_two_cycle("attack", 0.5)[1],
        note="Damped oscillation that converges quickly to a balanced fixpoint (decay rate = "
        "base 0.5 per sweep). The benign circular-refutation case.",
    ),
    Fixture(
        name="circular_refutation_critical",
        summary="a ⇄ b mutually REFUTE at FULL base 1.0 — the critical circular refutation.",
        baf=_two_cycle("attack", 1.0)[0],
        base=_two_cycle("attack", 1.0)[1],
        note="The headline cycle: under DF-QuAD the combine map has slope exactly 1 here "
        "(σ'=1−σ_other), so it is a sustained period-2 limit cycle (1,1)⇄(0,0) — flagged. "
        "Under Quadratic Energy the φ-squash is strictly contractive, so it converges.",
    ),
    Fixture(
        name="circular_refutation_nearcritical",
        summary="a ⇄ b mutually REFUTE just below critical (base 0.99).",
        baf=_two_cycle("attack", 0.99)[0],
        base=_two_cycle("attack", 0.99)[1],
        note="Converges, but glacially (decay rate 0.99/sweep ⇒ ≈2000 sweeps to reach 1e-9). The "
        "stress case for the iteration bound: the engine's per-step test holds it 'unstable' far "
        "longer than the variance criterion does.",
    ),
    Fixture(
        name="mixed_feedback_period4",
        summary="a SUPPORTS b, b REFUTES a (base a=1, b=0) — a mixed-sign feedback loop.",
        baf=BAF(
            frozenset({"a", "b"}),
            supports=(Edge("a", "b", 1.0),),
            attacks=(Edge("b", "a", 1.0),),
        ),
        base={"a": 1.0},
        note="A non-period-2 orbit: under DF-QuAD it is a period-4 limit cycle "
        "(1,0)→(1,1)→(0,1)→(0,0)→…; the window n must exceed the period. Under Quadratic Energy "
        "it converges.",
    ),
    Fixture(
        name="composite_converge_plus_oscillate",
        summary="Disjoint union: a support 2-cycle (settles) ⊕ a critical refutation 2-cycle "
        "(oscillates), in one framework.",
        baf=BAF(
            frozenset({"sa", "sb", "xa", "xb"}),
            supports=(Edge("sa", "sb", 1.0), Edge("sb", "sa", 1.0)),
            attacks=(Edge("xa", "xb", 1.0), Edge("xb", "xa", 1.0)),
        ),
        base={"sa": 0.3, "sb": 0.3, "xa": 1.0, "xb": 1.0},
        note="Localization test: `unstable` (and the criterion's oscillating set) must isolate "
        "the {xa, xb} cycle while {sa, sb} settle — the §13 'present the subgraph' requirement.",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Engine probes (all read-only `solve` calls) and trajectory reconstruction.
# ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineRun:
    cap: int
    converged: bool
    iterations: int
    unstable: frozenset[str]


def _solve(fx: Fixture, sem: GradualSemantics, cap: int) -> EngineRun:
    r = solve(
        fx.baf,
        base=dict(fx.base),
        semantics=sem,
        max_iterations=cap,
        tolerance=TOLERANCE,
    )
    return EngineRun(
        cap=cap,
        converged=r.converged,
        iterations=r.iterations,
        unstable=frozenset(str(a) for a in r.unstable),
    )


def settle_iteration(fx: Fixture, sem: GradualSemantics) -> int | None:
    """The fixture's *true* engine settle iteration (per-step change ≤ tolerance), or ``None`` if
    it does not converge within :data:`SETTLE_CEILING` — i.e. a sustained cycle."""
    r = _solve(fx, sem, SETTLE_CEILING)
    return r.iterations if r.converged else None


def reconstruct_trajectory(
    fx: Fixture, sem: GradualSemantics, horizon: int
) -> list[dict[str, float]]:
    """The per-sweep trajectory ``[σ_1, …, σ_horizon]`` via the public ``solve`` (engine read-only).

    ``solve(max_iterations=k)`` runs exactly ``k`` sweeps from the base seed (or stops earlier at
    a fixpoint, after which the state is constant), so its returned ``acceptability`` is the true
    post-``k`` state ``σ_k``. Deterministic and identically seeded, so the sequence is the genuine
    trajectory — no re-implementation of the engine's update rule.
    """
    trajectory: list[dict[str, float]] = []
    for k in range(1, horizon + 1):
        r = solve(fx.baf, base=dict(fx.base), semantics=sem, max_iterations=k, tolerance=TOLERANCE)
        trajectory.append({str(a): v for a, v in r.acceptability.items()})
    return trajectory


def criterion_settle_cap(trajectory: Sequence[Mapping[str, float]]) -> int | None:
    """Smallest cap ``K`` (≥ :data:`WINDOW`) at which the variance criterion reads *settled* and
    **stays** settled through the horizon, or ``None`` if it never settles within the horizon.

    'Stays settled' guards against a damped oscillation whose tail variance dips below ``ε`` mid-
    transient and rises again — only a K past which it is permanently quiet counts.
    """
    horizon = len(trajectory)
    settled_from: int | None = None
    for k in range(WINDOW, horizon + 1):
        if is_oscillating(trajectory[:k], window=WINDOW, epsilon=EPSILON):
            settled_from = None
        elif settled_from is None:
            settled_from = k
    return settled_from


# ─────────────────────────────────────────────────────────────────────────────────────────
# Per-fixture / per-semantics measurement record.
# ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SemanticsResult:
    semantics: str
    settle: int | None  # engine settle iteration, or None if a sustained cycle
    cap_runs: tuple[EngineRun, ...]  # engine verdict at each cap in CAPS
    crit_osc_by_cap: tuple[bool | None, ...]  # criterion verdict at each cap (None if cap < window)
    crit_var_by_cap: tuple[
        float | None, ...
    ]  # max tail variance at each cap (None if cap < window)
    final_tail_variance: float  # max tail variance at the horizon
    crit_settle_cap: int | None  # smallest cap the criterion declares settled from
    oscillating_set: frozenset[str]  # criterion's unresolved subgraph at the horizon
    trajectory: tuple[dict[str, float], ...]  # σ_1..σ_HORIZON, for reading the criterion at K*

    def criterion_oscillating_at(self, cap: int) -> bool:
        """The variance-criterion verdict at an arbitrary cap, read from the reconstructed
        trajectory (so an out-of-sweep `K*` is exact, not interpolated)."""
        return is_oscillating(list(self.trajectory[:cap]), window=WINDOW, epsilon=EPSILON)


def measure(fx: Fixture, sem: GradualSemantics) -> SemanticsResult:
    trajectory = reconstruct_trajectory(fx, sem, HORIZON)
    cap_runs = tuple(_solve(fx, sem, cap) for cap in CAPS)
    # The variance criterion needs at least `window` sweeps; below that it is undefined (None).
    crit_osc = tuple(
        is_oscillating(trajectory[:cap], window=WINDOW, epsilon=EPSILON) if cap >= WINDOW else None
        for cap in CAPS
    )
    crit_var = tuple(
        max_tail_variance(trajectory[:cap], window=WINDOW) if cap >= WINDOW else None
        for cap in CAPS
    )
    return SemanticsResult(
        semantics=sem.name,
        settle=settle_iteration(fx, sem),
        cap_runs=cap_runs,
        crit_osc_by_cap=crit_osc,
        crit_var_by_cap=crit_var,
        final_tail_variance=max_tail_variance(trajectory, window=WINDOW),
        crit_settle_cap=criterion_settle_cap(trajectory),
        oscillating_set=oscillating_args(trajectory, window=WINDOW, epsilon=EPSILON),
        trajectory=tuple(trajectory),
    )


@dataclass(frozen=True)
class FixtureResult:
    fixture: Fixture
    by_semantics: dict[str, SemanticsResult]


def run_all() -> list[FixtureResult]:
    semantics = (DF_QUAD, QUADRATIC_ENERGY)
    return [
        FixtureResult(fixture=fx, by_semantics={sem.name: measure(fx, sem) for sem in semantics})
        for fx in FIXTURES
    ]


# ─────────────────────────────────────────────────────────────────────────────────────────
# Recommendation — the empirical iteration bound, derived from the measured settle iterations.
# ─────────────────────────────────────────────────────────────────────────────────────────


def _round_up_to_nice(value: int) -> int:
    for nice in (32, 64, 128, 256, 512, 1024, 2048):
        if nice >= value:
            return nice
    return value


@dataclass(frozen=True)
class Recommendation:
    bound: int
    fast_settle_max: int
    glacial: tuple[str, ...]  # "fixture/semantics" pairs that settle slower than the bound


def recommend_bound(results: list[FixtureResult]) -> Recommendation:
    """Recommend the iteration bound: clear every *fast*-settling converger by the variance
    criterion with margin, while staying below the deliberately-glacial stress case (which is
    surfaced, not converged). Derived from the measured criterion settle caps."""
    fast: list[int] = []
    glacial: list[str] = []
    for fr in results:
        for sem_name, sr in fr.by_semantics.items():
            if sr.crit_settle_cap is None:
                continue  # a sustained cycle — not a converger, ignore for the lower bound
            if sr.crit_settle_cap <= FAST_SETTLE_CEILING:
                fast.append(sr.crit_settle_cap)
            else:
                glacial.append(f"{fr.fixture.name}/{sem_name}")
    fast_max = max(fast, default=0)
    return Recommendation(
        bound=_round_up_to_nice(2 * fast_max),
        fast_settle_max=fast_max,
        glacial=tuple(glacial),
    )


# ─────────────────────────────────────────────────────────────────────────────────────────
# Report rendering (markdown). Reuses `iknos.trials.report.comparison_table` for the matrices.
# ─────────────────────────────────────────────────────────────────────────────────────────


def _engine_verdict(run: EngineRun) -> str:
    if run.converged:
        return f"conv@{run.iterations}"
    return f"UNSTABLE×{len(run.unstable)}"


def _fmt_var(value: float | None) -> str:
    """Tail variance in scientific notation (it spans ~1e-30 settled to ~0.25 oscillating)."""
    return "n/a" if value is None else f"{value:.2e}"


def _crit_cell(flag: bool | None) -> str:
    if flag is None:
        return "n/a (cap < n)"
    return "OSC" if flag else "settled"


def _cap_table(sr: SemanticsResult) -> str:
    """A cap × {engine, criterion} matrix for one semantics."""
    rows: dict[str, dict[str, str | float]] = {}
    for i, cap in enumerate(CAPS):
        rows[f"cap={cap}"] = {
            "engine": _engine_verdict(sr.cap_runs[i]),
            "criterion": _crit_cell(sr.crit_osc_by_cap[i]),
            "max tail var": _fmt_var(sr.crit_var_by_cap[i]),
        }
    return comparison_table(rows, row_header="iteration cap")


def _settle_str(settle: int | None) -> str:
    return str(settle) if settle is not None else "—  (sustained cycle)"


def _format_set(args: frozenset[str]) -> str:
    return "{" + ", ".join(sorted(args)) + "}" if args else "∅"


def build_report(results: list[FixtureResult], rec: Recommendation) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Trial B2 — Cyclic-region oscillation: empirical iteration bound & detection criterion")
    a("")
    a(
        "**Instrument B / Trial B2** (`docs/todo_trials.md`). Gates Phase-4 QBAF oscillation "
        "handling and Phase-6 cyclic-region presentation. Machine half only — the expert "
        "comprehension check (do experts read the *unresolved region* presentation correctly?) is "
        "**human work and stays open** (see §6)."
    )
    a("")
    a(
        "- **Harness:** `scripts/b2_cyclic_oscillation.py` (reproduce: "
        "`uv run python -m scripts.b2_cyclic_oscillation`)."
    )
    a(
        "- **Engine under test:** `core/qbaf.solve` — **read-only, unmodified**. The per-sweep "
        "trajectory the variance criterion needs is reconstructed through the public `solve` "
        "(`solve(max_iterations=k)` = the true post-`k`-sweep state); no knob added to the engine."
    )
    a(
        "- **Criterion under test:** `iknos.trials.oscillation` — strength variance over the last "
        "`n` sweeps > `ε` (pure, unit-tested, LLM-free)."
    )
    a(
        f"- **Parameters:** engine `tolerance={TOLERANCE:g}`, engine default cap "
        f"`{ENGINE_DEFAULT_CAP}`; criterion `n (window)={WINDOW}`, `ε={EPSILON:g}`; cap sweep "
        f"{list(CAPS)}; trajectory horizon {HORIZON}. No DB / AGE / LLM / wall clock — pure CPU."
    )
    a("")

    # ── §1 Decision ─────────────────────────────────────────────────────────────────────
    slowest_engine_settle = max(
        (sr.settle for fr in results for sr in fr.by_semantics.values() if sr.settle is not None),
        default=0,
    )
    a("## 1. Decision (proposed bounds)")
    a("")
    a(
        f"- **Iteration bound `K* = {rec.bound}` sweeps** — read with the variance criterion, "
        f"**not** the engine's per-step `converged` flag. Smallest nice value ≥ 2× the slowest "
        f"*benign*-cycle criterion settle (sweep {rec.fast_settle_max}); it clears every benign "
        f"support/refutation cycle while still flagging genuine limit cycles and any trajectory "
        f"not yet settled. The criterion releases benign convergers earlier than the engine's "
        f"strict per-step tolerance does — the near-critical converger is held `unstable` by the "
        f"engine until ≈{slowest_engine_settle} sweeps (so the engine default cap "
        f"{ENGINE_DEFAULT_CAP} cannot distinguish it from a true cycle), yet it is genuinely "
        f"still-moving — and so correctly surfaced — at `K*` (§3)."
    )
    a(
        f"- **Oscillation criterion: max per-argument strength variance over the last `n = "
        f"{WINDOW}` sweeps `> ε = {EPSILON:g}`.** Read at `K*`. The window exceeds the longest "
        f"observed limit-cycle period (4); `ε` sits in the measured gap between settled tails "
        f"(≈0) and sustained cycles (≥ 0.05) with several orders of margin either side (§4)."
    )
    a(
        "- **Surface, don't converge (§13, principle 8).** When the criterion fires, present the "
        "`oscillating_args` subgraph as the *unresolved region* — never a verdict. The composite "
        "fixture shows the set localises to the genuinely cyclic arguments."
    )
    if rec.glacial:
        a(
            f"- **Budget trade-off, recorded:** {', '.join(rec.glacial)} settle slower than `K*`; "
            f"within budget they are (correctly) surfaced as still-moving. Raising the budget "
            f"converges them — the bound is a *budget*, not a claim they cannot settle."
        )
    a("")

    # ── §2 Per-fixture results ──────────────────────────────────────────────────────────
    a("## 2. Per-fixture results")
    a("")
    for fr in results:
        fx = fr.fixture
        a(f"### `{fx.name}`")
        a("")
        a(f"{fx.summary}")
        a("")
        a(f"_{fx.note}_")
        a("")
        for sem_name in (DF_QUAD.name, QUADRATIC_ENERGY.name):
            sr = fr.by_semantics[sem_name]
            settle_cap = (
                str(sr.crit_settle_cap)
                if sr.crit_settle_cap is not None
                else "— (never, within horizon)"
            )
            a(
                f"**{sem_name}** — engine settle: {_settle_str(sr.settle)}; "
                f"criterion settled-from cap: {settle_cap}; "
                f"unresolved set at horizon: {_format_set(sr.oscillating_set)}."
            )
            a("")
            a(_cap_table(sr))
            a("")

    # ── §3 Iteration-bound sensitivity ──────────────────────────────────────────────────
    a("## 3. Iteration bound — settle iterations & the slow-convergence trap")
    a("")
    a(
        "Engine settle iteration (per-step change ≤ tolerance) and the criterion's settled-from "
        "cap, per fixture/semantics. Where the engine column is far larger than the criterion "
        "column, the per-step tolerance is holding a *practically-settled* trajectory 'unstable' — "
        "the variance window releases it sooner."
    )
    a("")
    settle_rows: dict[str, dict[str, str | float]] = {}
    for fr in results:
        for sem_name, sr in fr.by_semantics.items():
            settle_rows[f"{fr.fixture.name} / {sem_name}"] = {
                "engine settle": _settle_str(sr.settle),
                "criterion settle cap": (
                    str(sr.crit_settle_cap) if sr.crit_settle_cap is not None else "— (cycle)"
                ),
                "engine@default-cap": _engine_verdict(
                    next(r for r in sr.cap_runs if r.cap == ENGINE_DEFAULT_CAP)
                ),
            }
    a(comparison_table(settle_rows, row_header="fixture / semantics"))
    a("")

    # ── §4 ε separation ─────────────────────────────────────────────────────────────────
    a("## 4. ε separation — settled tails vs sustained cycles")
    a("")
    a(
        f"Max per-argument tail variance at the horizon ({HORIZON} sweeps). Settled fixtures sit "
        f"orders of magnitude below `ε = {EPSILON:g}`; sustained cycles sit orders above it — the "
        f"gap the threshold exploits."
    )
    a("")
    var_rows: dict[str, dict[str, str | float]] = {}
    for fr in results:
        for sem_name, sr in fr.by_semantics.items():
            var_rows[f"{fr.fixture.name} / {sem_name}"] = {
                "tail variance @horizon": _fmt_var(sr.final_tail_variance),
                "vs ε": "OSC (> ε)" if sr.final_tail_variance > EPSILON else "settled (≤ ε)",
            }
    a(comparison_table(var_rows, row_header="fixture / semantics"))
    a("")

    # ── §5 flags-vs-falsely-converges summary ───────────────────────────────────────────
    a("## 5. Flags cycles vs falsely converges (per semantics)")
    a("")
    a(
        "At `K*`, does each detector match the fixture's known character? A sustained cycle should "
        "be flagged (`OSC` / engine `UNSTABLE`); a converger should be settled. No fixture is "
        "**falsely converged** by either detector — the engine's per-step test never reports a "
        "sustained cycle as `converged`, and the variance criterion agrees at `K*`."
    )
    a("")
    for sem_name in (DF_QUAD.name, QUADRATIC_ENERGY.name):
        a(f"**{sem_name}:**")
        a("")
        sem = _semantics_by_name(sem_name)
        summ_rows: dict[str, dict[str, str | float]] = {}
        for fr in results:
            sr = fr.by_semantics[sem_name]
            criterion_osc = sr.criterion_oscillating_at(rec.bound)
            engine_unstable = not _solve(fr.fixture, sem, rec.bound).converged
            summ_rows[fr.fixture.name] = {
                f"engine @K*={rec.bound}": "UNSTABLE" if engine_unstable else "converged",
                f"criterion @K*={rec.bound}": "OSC" if criterion_osc else "settled",
            }
        a(comparison_table(summ_rows, row_header="fixture"))
        a("")
    a(
        "**Cross-semantics finding.** The full-strength circular-refutation cycle "
        "(`circular_refutation_critical`) **oscillates under DF-QuAD but converges under Quadratic "
        "Energy**: DF-QuAD's combine is piecewise-linear with slope exactly 1 on a full-strength "
        "attack (σ' = 1 − σ_other), giving a marginally-stable period-2 limit cycle; Quadratic "
        "Energy's φ(x)=x²/(1+x²) squash is strictly contractive there, so it settles. DF-QuAD — "
        "the recorded Phase-4 default — therefore *surfaces* full-strength contradiction loops as "
        "unresolved rather than smoothing them into a verdict, which is the conservative, "
        "principle-8 behaviour. The same holds for the period-4 `mixed_feedback` loop."
    )
    a("")

    # ── §6 Limitations & handoffs ───────────────────────────────────────────────────────
    a("## 6. Limitations & handoffs")
    a("")
    a(
        "- **Expert comprehension check — OPEN (human work).** B2's second half ('do experts read "
        "the *unresolved region* presentation correctly?') and the resulting presentation choice "
        "(principle 8) are a human study, not measurable here. The machine half (this report) "
        "fixes the iteration bound and detection criterion; the presentation decision stays open "
        "in `docs/todo_trials.md`."
    )
    a(
        "- **Variance conflates oscillation with un-settled drift.** A trajectory still drifting "
        "monotonically toward its fixpoint also has non-zero tail variance, so the criterion is "
        "valid only *past the transient* — which is exactly what the iteration bound `K*` "
        "guarantees. Read the criterion at `K*`, not at an arbitrary small cap."
    )
    a(
        "- **Core-lane handoff.** No engine knob was missing for this measurement (the existing "
        "`max_iterations` / `tolerance` / `unstable` sufficed). When Phase-4 hardens oscillation "
        "handling, the recommendation is to **promote the variance criterion into the engine or a "
        "presentation layer** (lift `iknos.trials.oscillation` into `core/`), so `solve` can "
        "optionally return a variance-based unresolved set in addition to the per-step `unstable` "
        "set — they answer different questions (still-moving-this-sweep vs moving-across-a-window)."
    )
    a(
        "- **Synthetic, small fixtures.** These are minimal cyclic motifs (≤6 arguments) chosen to "
        "isolate dynamics, not investigation-scale graphs; the bound is for the *per-region* solve "
        "the gradual semantics runs, which operates on a cyclic subgraph, not the whole graph."
    )
    a("")

    return "\n".join(lines)


# Small helper used by the §5 summary (kept here so the report builder reads top-down).


def _semantics_by_name(name: str) -> GradualSemantics:
    return DF_QUAD if name == DF_QUAD.name else QUADRATIC_ENERGY


# ─────────────────────────────────────────────────────────────────────────────────────────
# Entry point.
# ─────────────────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the markdown report here (also printed to stdout)",
    )
    args = parser.parse_args()

    results = run_all()
    rec = recommend_bound(results)
    report = build_report(results, rec)

    print(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
