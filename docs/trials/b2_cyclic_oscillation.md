# Trial B2 — Cyclic-region oscillation: empirical iteration bound & detection criterion

**Instrument B / Trial B2** (`docs/todo_trials.md`). Gates Phase-4 QBAF oscillation handling and Phase-6 cyclic-region presentation. Machine half only — the expert comprehension check (do experts read the *unresolved region* presentation correctly?) is **human work and stays open** (see §6).

- **Harness:** `scripts/b2_cyclic_oscillation.py` (reproduce: `uv run python -m scripts.b2_cyclic_oscillation`).
- **Engine under test:** `core/qbaf.solve` — **read-only, unmodified**. The per-sweep trajectory the variance criterion needs is reconstructed through the public `solve` (`solve(max_iterations=k)` = the true post-`k`-sweep state); no knob added to the engine.
- **Criterion under test:** `iknos.trials.oscillation` — strength variance over the last `n` sweeps > `ε` (pure, unit-tested, LLM-free).
- **Parameters:** engine `tolerance=1e-09`, engine default cap `100`; criterion `n (window)=16`, `ε=1e-06`; cap sweep [10, 20, 50, 100, 200, 400, 600, 800]; trajectory horizon 800. No DB / AGE / LLM / wall clock — pure CPU.

## 1. Decision (proposed bounds)

- **Iteration bound `K* = 64` sweeps** — read with the variance criterion, **not** the engine's per-step `converged` flag. Smallest nice value ≥ 2× the slowest *benign*-cycle criterion settle (sweep 30); it clears every benign support/refutation cycle while still flagging genuine limit cycles and any trajectory not yet settled. The criterion releases benign convergers earlier than the engine's strict per-step tolerance does — the near-critical converger is held `unstable` by the engine until ≈2061 sweeps (so the engine default cap 100 cannot distinguish it from a true cycle), yet it is genuinely still-moving — and so correctly surfaced — at `K*` (§3).
- **Oscillation criterion: max per-argument strength variance over the last `n = 16` sweeps `> ε = 1e-06`.** Read at `K*`. The window exceeds the longest observed limit-cycle period (4); `ε` sits in the measured gap between settled tails (≈0) and sustained cycles (≥ 0.05) with several orders of margin either side (§4).
- **Surface, don't converge (§13, principle 8).** When the criterion fires, present the `oscillating_args` subgraph as the *unresolved region* — never a verdict. The composite fixture shows the set localises to the genuinely cyclic arguments.
- **Budget trade-off, recorded:** circular_refutation_nearcritical/df-quad settle slower than `K*`; within budget they are (correctly) surfaced as still-moving. Raising the budget converges them — the bound is a *budget*, not a claim they cannot settle.

## 2. Per-fixture results

### `acyclic_control`

Acyclic chain p→q→h (all support) — the non-cyclic baseline.

_Converges to its exact fixpoint in depth-many sweeps; must never be flagged._

**df-quad** — engine settle: 3; criterion settled-from cap: 17; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | conv@3 | n/a (cap < n) | n/a |
| cap=20 | conv@3 | settled | 0.00e+00 |
| cap=50 | conv@3 | settled | 0.00e+00 |
| cap=100 | conv@3 | settled | 0.00e+00 |
| cap=200 | conv@3 | settled | 0.00e+00 |
| cap=400 | conv@3 | settled | 0.00e+00 |
| cap=600 | conv@3 | settled | 0.00e+00 |
| cap=800 | conv@3 | settled | 0.00e+00 |

**quadratic-energy** — engine settle: 3; criterion settled-from cap: 17; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | conv@3 | n/a (cap < n) | n/a |
| cap=20 | conv@3 | settled | 0.00e+00 |
| cap=50 | conv@3 | settled | 0.00e+00 |
| cap=100 | conv@3 | settled | 0.00e+00 |
| cap=200 | conv@3 | settled | 0.00e+00 |
| cap=400 | conv@3 | settled | 0.00e+00 |
| cap=600 | conv@3 | settled | 0.00e+00 |
| cap=800 | conv@3 | settled | 0.00e+00 |

### `mutual_support_2cycle`

a ⇄ b mutually SUPPORT (base 0.3) — a reinforcing cycle.

_Reinforcing support converges to a saturated high fixpoint; a support cycle is *not* an oscillation and must not be flagged — but it converges only geometrically, so a too-low cap would false-flag it._

**df-quad** — engine settle: 55; criterion settled-from cap: 30; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | OSC | 1.10e-03 |
| cap=50 | UNSTABLE×2 | settled | 5.59e-13 |
| cap=100 | conv@55 | settled | 0.00e+00 |
| cap=200 | conv@55 | settled | 0.00e+00 |
| cap=400 | conv@55 | settled | 0.00e+00 |
| cap=600 | conv@55 | settled | 0.00e+00 |
| cap=800 | conv@55 | settled | 0.00e+00 |

**quadratic-energy** — engine settle: 22; criterion settled-from cap: 19; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | settled | 6.68e-08 |
| cap=50 | conv@22 | settled | 0.00e+00 |
| cap=100 | conv@22 | settled | 0.00e+00 |
| cap=200 | conv@22 | settled | 0.00e+00 |
| cap=400 | conv@22 | settled | 0.00e+00 |
| cap=600 | conv@22 | settled | 0.00e+00 |
| cap=800 | conv@22 | settled | 0.00e+00 |

### `circular_refutation_damped`

a ⇄ b mutually REFUTE (base 0.5) — sub-critical circular refutation.

_Damped oscillation that converges quickly to a balanced fixpoint (decay rate = base 0.5 per sweep). The benign circular-refutation case._

**df-quad** — engine settle: 29; criterion settled-from cap: 21; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | OSC | 2.21e-06 |
| cap=50 | conv@29 | settled | 0.00e+00 |
| cap=100 | conv@29 | settled | 0.00e+00 |
| cap=200 | conv@29 | settled | 0.00e+00 |
| cap=400 | conv@29 | settled | 0.00e+00 |
| cap=600 | conv@29 | settled | 0.00e+00 |
| cap=800 | conv@29 | settled | 0.00e+00 |

**quadratic-energy** — engine settle: 17; criterion settled-from cap: 18; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | conv@17 | settled | 2.76e-09 |
| cap=50 | conv@17 | settled | 0.00e+00 |
| cap=100 | conv@17 | settled | 0.00e+00 |
| cap=200 | conv@17 | settled | 0.00e+00 |
| cap=400 | conv@17 | settled | 0.00e+00 |
| cap=600 | conv@17 | settled | 0.00e+00 |
| cap=800 | conv@17 | settled | 0.00e+00 |

### `circular_refutation_critical`

a ⇄ b mutually REFUTE at FULL base 1.0 — the critical circular refutation.

_The headline cycle: under DF-QuAD the combine map has slope exactly 1 here (σ'=1−σ_other), so it is a sustained period-2 limit cycle (1,1)⇄(0,0) — flagged. Under Quadratic Energy the φ-squash is strictly contractive, so it converges._

**df-quad** — engine settle: —  (sustained cycle); criterion settled-from cap: — (never, within horizon); unresolved set at horizon: {a, b}.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=50 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=100 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=200 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=400 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=600 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=800 | UNSTABLE×2 | OSC | 2.50e-01 |

**quadratic-energy** — engine settle: 45; criterion settled-from cap: 25; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | OSC | 8.92e-05 |
| cap=50 | conv@45 | settled | 1.35e-16 |
| cap=100 | conv@45 | settled | 0.00e+00 |
| cap=200 | conv@45 | settled | 0.00e+00 |
| cap=400 | conv@45 | settled | 0.00e+00 |
| cap=600 | conv@45 | settled | 0.00e+00 |
| cap=800 | conv@45 | settled | 0.00e+00 |

### `circular_refutation_nearcritical`

a ⇄ b mutually REFUTE just below critical (base 0.99).

_Converges, but glacially (decay rate 0.99/sweep ⇒ ≈2000 sweeps to reach 1e-9). The stress case for the iteration bound: the engine's per-step test holds it 'unstable' far longer than the variance criterion does._

**df-quad** — engine settle: 2061; criterion settled-from cap: 625; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | OSC | 1.89e-01 |
| cap=50 | UNSTABLE×2 | OSC | 1.04e-01 |
| cap=100 | UNSTABLE×2 | OSC | 3.79e-02 |
| cap=200 | UNSTABLE×2 | OSC | 5.08e-03 |
| cap=400 | UNSTABLE×2 | OSC | 9.13e-05 |
| cap=600 | UNSTABLE×2 | OSC | 1.64e-06 |
| cap=800 | UNSTABLE×2 | settled | 2.94e-08 |

**quadratic-energy** — engine settle: 45; criterion settled-from cap: 25; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×2 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×2 | OSC | 7.88e-05 |
| cap=50 | conv@45 | settled | 7.20e-17 |
| cap=100 | conv@45 | settled | 0.00e+00 |
| cap=200 | conv@45 | settled | 0.00e+00 |
| cap=400 | conv@45 | settled | 0.00e+00 |
| cap=600 | conv@45 | settled | 0.00e+00 |
| cap=800 | conv@45 | settled | 0.00e+00 |

### `mixed_feedback_period4`

a SUPPORTS b, b REFUTES a (base a=1, b=0) — a mixed-sign feedback loop.

_A non-period-2 orbit: under DF-QuAD it is a period-4 limit cycle (1,0)→(1,1)→(0,1)→(0,0)→…; the window n must exceed the period. Under Quadratic Energy it converges._

**df-quad** — engine settle: —  (sustained cycle); criterion settled-from cap: — (never, within horizon); unresolved set at horizon: {a, b}.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×1 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×1 | OSC | 2.50e-01 |
| cap=50 | UNSTABLE×1 | OSC | 2.50e-01 |
| cap=100 | UNSTABLE×1 | OSC | 2.50e-01 |
| cap=200 | UNSTABLE×1 | OSC | 2.50e-01 |
| cap=400 | UNSTABLE×1 | OSC | 2.50e-01 |
| cap=600 | UNSTABLE×1 | OSC | 2.50e-01 |
| cap=800 | UNSTABLE×1 | OSC | 2.50e-01 |

**quadratic-energy** — engine settle: 39; criterion settled-from cap: 23; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×1 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×1 | OSC | 2.42e-05 |
| cap=50 | conv@39 | settled | 3.96e-19 |
| cap=100 | conv@39 | settled | 0.00e+00 |
| cap=200 | conv@39 | settled | 0.00e+00 |
| cap=400 | conv@39 | settled | 0.00e+00 |
| cap=600 | conv@39 | settled | 0.00e+00 |
| cap=800 | conv@39 | settled | 0.00e+00 |

### `composite_converge_plus_oscillate`

Disjoint union: a support 2-cycle (settles) ⊕ a critical refutation 2-cycle (oscillates), in one framework.

_Localization test: `unstable` (and the criterion's oscillating set) must isolate the {xa, xb} cycle while {sa, sb} settle — the §13 'present the subgraph' requirement._

**df-quad** — engine settle: —  (sustained cycle); criterion settled-from cap: — (never, within horizon); unresolved set at horizon: {xa, xb}.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×4 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×4 | OSC | 2.50e-01 |
| cap=50 | UNSTABLE×4 | OSC | 2.50e-01 |
| cap=100 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=200 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=400 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=600 | UNSTABLE×2 | OSC | 2.50e-01 |
| cap=800 | UNSTABLE×2 | OSC | 2.50e-01 |

**quadratic-energy** — engine settle: 45; criterion settled-from cap: 25; unresolved set at horizon: ∅.

| iteration cap | engine | criterion | max tail var |
| --- | --- | --- | --- |
| cap=10 | UNSTABLE×4 | n/a (cap < n) | n/a |
| cap=20 | UNSTABLE×4 | OSC | 8.92e-05 |
| cap=50 | conv@45 | settled | 1.35e-16 |
| cap=100 | conv@45 | settled | 0.00e+00 |
| cap=200 | conv@45 | settled | 0.00e+00 |
| cap=400 | conv@45 | settled | 0.00e+00 |
| cap=600 | conv@45 | settled | 0.00e+00 |
| cap=800 | conv@45 | settled | 0.00e+00 |

## 3. Iteration bound — settle iterations & the slow-convergence trap

Engine settle iteration (per-step change ≤ tolerance) and the criterion's settled-from cap, per fixture/semantics. Where the engine column is far larger than the criterion column, the per-step tolerance is holding a *practically-settled* trajectory 'unstable' — the variance window releases it sooner.

| fixture / semantics | engine settle | criterion settle cap | engine@default-cap |
| --- | --- | --- | --- |
| acyclic_control / df-quad | 3 | 17 | conv@3 |
| acyclic_control / quadratic-energy | 3 | 17 | conv@3 |
| mutual_support_2cycle / df-quad | 55 | 30 | conv@55 |
| mutual_support_2cycle / quadratic-energy | 22 | 19 | conv@22 |
| circular_refutation_damped / df-quad | 29 | 21 | conv@29 |
| circular_refutation_damped / quadratic-energy | 17 | 18 | conv@17 |
| circular_refutation_critical / df-quad | —  (sustained cycle) | — (cycle) | UNSTABLE×2 |
| circular_refutation_critical / quadratic-energy | 45 | 25 | conv@45 |
| circular_refutation_nearcritical / df-quad | 2061 | 625 | UNSTABLE×2 |
| circular_refutation_nearcritical / quadratic-energy | 45 | 25 | conv@45 |
| mixed_feedback_period4 / df-quad | —  (sustained cycle) | — (cycle) | UNSTABLE×1 |
| mixed_feedback_period4 / quadratic-energy | 39 | 23 | conv@39 |
| composite_converge_plus_oscillate / df-quad | —  (sustained cycle) | — (cycle) | UNSTABLE×2 |
| composite_converge_plus_oscillate / quadratic-energy | 45 | 25 | conv@45 |

## 4. ε separation — settled tails vs sustained cycles

Max per-argument tail variance at the horizon (800 sweeps). Settled fixtures sit orders of magnitude below `ε = 1e-06`; sustained cycles sit orders above it — the gap the threshold exploits.

| fixture / semantics | tail variance @horizon | vs ε |
| --- | --- | --- |
| acyclic_control / df-quad | 0.00e+00 | settled (≤ ε) |
| acyclic_control / quadratic-energy | 0.00e+00 | settled (≤ ε) |
| mutual_support_2cycle / df-quad | 0.00e+00 | settled (≤ ε) |
| mutual_support_2cycle / quadratic-energy | 0.00e+00 | settled (≤ ε) |
| circular_refutation_damped / df-quad | 0.00e+00 | settled (≤ ε) |
| circular_refutation_damped / quadratic-energy | 0.00e+00 | settled (≤ ε) |
| circular_refutation_critical / df-quad | 2.50e-01 | OSC (> ε) |
| circular_refutation_critical / quadratic-energy | 0.00e+00 | settled (≤ ε) |
| circular_refutation_nearcritical / df-quad | 2.94e-08 | settled (≤ ε) |
| circular_refutation_nearcritical / quadratic-energy | 0.00e+00 | settled (≤ ε) |
| mixed_feedback_period4 / df-quad | 2.50e-01 | OSC (> ε) |
| mixed_feedback_period4 / quadratic-energy | 0.00e+00 | settled (≤ ε) |
| composite_converge_plus_oscillate / df-quad | 2.50e-01 | OSC (> ε) |
| composite_converge_plus_oscillate / quadratic-energy | 0.00e+00 | settled (≤ ε) |

## 5. Flags cycles vs falsely converges (per semantics)

At `K*`, does each detector match the fixture's known character? A sustained cycle should be flagged (`OSC` / engine `UNSTABLE`); a converger should be settled. No fixture is **falsely converged** by either detector — the engine's per-step test never reports a sustained cycle as `converged`, and the variance criterion agrees at `K*`.

**df-quad:**

| fixture | engine @K*=64 | criterion @K*=64 |
| --- | --- | --- |
| acyclic_control | converged | settled |
| mutual_support_2cycle | converged | settled |
| circular_refutation_damped | converged | settled |
| circular_refutation_critical | UNSTABLE | OSC |
| circular_refutation_nearcritical | UNSTABLE | OSC |
| mixed_feedback_period4 | UNSTABLE | OSC |
| composite_converge_plus_oscillate | UNSTABLE | OSC |

**quadratic-energy:**

| fixture | engine @K*=64 | criterion @K*=64 |
| --- | --- | --- |
| acyclic_control | converged | settled |
| mutual_support_2cycle | converged | settled |
| circular_refutation_damped | converged | settled |
| circular_refutation_critical | converged | settled |
| circular_refutation_nearcritical | converged | settled |
| mixed_feedback_period4 | converged | settled |
| composite_converge_plus_oscillate | converged | settled |

**Cross-semantics finding.** The full-strength circular-refutation cycle (`circular_refutation_critical`) **oscillates under DF-QuAD but converges under Quadratic Energy**: DF-QuAD's combine is piecewise-linear with slope exactly 1 on a full-strength attack (σ' = 1 − σ_other), giving a marginally-stable period-2 limit cycle; Quadratic Energy's φ(x)=x²/(1+x²) squash is strictly contractive there, so it settles. DF-QuAD — the recorded Phase-4 default — therefore *surfaces* full-strength contradiction loops as unresolved rather than smoothing them into a verdict, which is the conservative, principle-8 behaviour. The same holds for the period-4 `mixed_feedback` loop.

## 6. Limitations & handoffs

- **Expert comprehension check — OPEN (human work).** B2's second half ('do experts read the *unresolved region* presentation correctly?') and the resulting presentation choice (principle 8) are a human study, not measurable here. The machine half (this report) fixes the iteration bound and detection criterion; the presentation decision stays open in `docs/todo_trials.md`.
- **Variance conflates oscillation with un-settled drift.** A trajectory still drifting monotonically toward its fixpoint also has non-zero tail variance, so the criterion is valid only *past the transient* — which is exactly what the iteration bound `K*` guarantees. Read the criterion at `K*`, not at an arbitrary small cap.
- **Core-lane handoff.** No engine knob was missing for this measurement (the existing `max_iterations` / `tolerance` / `unstable` sufficed). When Phase-4 hardens oscillation handling, the recommendation is to **promote the variance criterion into the engine or a presentation layer** (lift `iknos.trials.oscillation` into `core/`), so `solve` can optionally return a variance-based unresolved set in addition to the per-step `unstable` set — they answer different questions (still-moving-this-sweep vs moving-across-a-window).
- **Synthetic, small fixtures.** These are minimal cyclic motifs (≤6 arguments) chosen to isolate dynamics, not investigation-scale graphs; the bound is for the *per-region* solve the gradual semantics runs, which operates on a cyclic subgraph, not the whole graph.

