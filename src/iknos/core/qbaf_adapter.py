"""Phase 4 adjudication adapter (G4.4; architecture §5, §7.2, §8, §10, §11.2).

The pure QBAF engine (``core/qbaf.py``) operates on an **abstract** :class:`~iknos.core.qbaf.BAF`
of opaque ``ArgId`` strings + a ``base`` score map — it knows nothing of AGE, UUIDs, boxes, or
bitemporal validity. This module is the **boundary** that reads the persisted property graph and
produces exactly those two inputs, then writes the computed verdict back. It is the Phase-4
analogue of ``core/derivation_adapter.py`` (G3.4): same pure/DB split, same active-subgraph
discipline, same lazy ``iknos.db.age`` import.

**The two inputs (§12 seam).** Layer A decides well-founded membership, Layer B scores it, and
the QBAF adjudicates supports/refutes over those scores — so the QBAF's intrinsic/**base score**
for every argument is that node's Layer B ``confidence`` property (§12: "Layer B's ``[0, 1]``
confidence is the clean strength consumed as a node's intrinsic/base score by the QBAF gradual
semantics"), and the ``SUPPORTS``/``REFUTES`` edges carry the §7.1 calibrated ``strength``.

**Edge direction is fixed by the schema (§5, §10).** A ``SUPPORTS``/``REFUTES`` edge runs
**Fact/Conclusion (the evidence) → Hypothesis (or Conclusion)**. So the edge ``source`` is the
supporter/attacker that lends strength and the ``target`` is the argument receiving it; the
adapter maps ``source → Edge.src``, ``target → Edge.dst``, and the **sign** to the support vs
attack collection (§8 "sign before magnitude" — direction is categorical, modelled
structurally, never a signed number).

**The active subgraph (§10, §12).** As in G3.4, reasoning is over the *current* belief state:
only bitemporally-current nodes/edges (``valid_to IS NULL`` — a retraction drops out) in
**active** boxes. An evidential edge with an inactive (retracted / deprecated-box) endpoint is
**dropped** — a dead supporter lends nothing. (This is the *opposite* polarity to the derivation
adapter, which keeps an inactive antecedent in a conjunctive body so the rule gets *harder*:
QBAF support is additive, so a vanished supporter must simply contribute nothing.)

**Write-back (§10, §11.2, §7.2).** ``persist_verdicts`` writes each ``Hypothesis``'s computed
``acceptability`` (the real-valued QBAF strength) and ``state`` (``supported``/``unsupported``/
``refuted``) — **computed, never hand-set**. It uses a *partial* ``SET h.acceptability=…,
h.state=…`` (not ``merge_vertex``'s full ``SET n = {…}``, which would clobber the node's
bitemporal/confidence fields). The presentation **band** is *not* stored — per
``types/intentional.py`` it is computed from the strength at render time, never a stored
substitute for the real value (§11.2).

**The §7.2 ensemble gate, made structural in the writer (V8).** A flip *to* ``refuted`` requires
the ensemble gate (multi-sample LLM + symbolic + temporal agreement, ``core/ensemble_gate``),
never a single judgment. ``persist_verdicts`` takes ``gate_decisions`` (hypothesis id →
:class:`~iknos.core.ensemble_gate.GateDecision`, from ``ensemble_gate.authorise``): a computed
``refuted`` verdict is persisted as ``refuted`` **only** with an *authorising* decision; otherwise
the flip is **held** — ``acceptability`` is still written, but ``state`` keeps the hypothesis's
**prior** value and ``pending_refutation`` is flagged, surfaced as a §13 finding
(:attr:`PersistResult.is_finding`, reason ``ensemble_gate_pending``) rather than silently flipped
*or* dropped. The hold clears on any later verdict that persists a non-refuted or
authorised-refuted state. So ``refuted`` is **unreachable through this writer without an
authorising ``GateDecision``** — ``ensemble_gate.authorise`` is the only intended producer of one
(the §7.2 invariant; this is the consumer-side mirror of the V7 producer-side quarantine). This is
the only code path that writes ``Hypothesis.state``.

Scope deliberately left to later increments (documented seams):

- **The edge-judgment pipeline (G4.3)** that *produces* calibrated ``SUPPORTS``/``REFUTES``
  edges from LLM judgments (sign-before-magnitude, blind/randomized, multi-sample, subjective-
  logic fusion, §8) — this adapter *consumes* those edges. Like G3.4 defined the
  ``DERIVED_FROM`` contract before G3.8 wrote it, this defines the load/write contract before
  G4.3 fills it; the unit tests exercise it with hand-built rows.
- **The gate's channel producers (later G4.5 slices)** — V8 consumes ``GateDecision``s; the
  symbolic (clingo/ASP) and temporal channels that *produce* the affirming/dissenting signals
  feeding ``ensemble_gate.authorise`` are later slices (until then ``DEFAULT_GATE`` withholds,
  safe-by-default). The caller assembles ``gate_decisions`` from those signals; this writer only
  honours the verdict.
- **Incremental QBAF update** (§13, an apparent open research gap) — this is a *full* recompute
  over the active subgraph (acceptable at investigation scale, §13). The composed-loop body
  (``REFUTES → retract → A → B → QBAF``, G4.5 + the G3.9 driver) wraps this evaluate step.
- **``SAME_AS``-component aggregation** of evidential edges — as in G3.4, raw nodes are loaded;
  canonicalising arguments by entity (G3.7's analogue for adjudication) is deferred.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from iknos.core.derivation_adapter import (
    NodeRow,
    load_active_box_ids,
    load_hypothesis_ids,
    load_reasoning_nodes,
)
from iknos.core.ensemble_gate import GateDecision
from iknos.core.qbaf import (
    BAF,
    DEFAULT_SEMANTICS,
    ArgId,
    Edge,
    GradualSemantics,
    Strength,
    aggregate_evidence,
    classify_state,
    solve,
)
from iknos.types.edges import EdgeSign
from iknos.types.intentional import AcceptabilityBand, HypothesisState, band


@dataclass(frozen=True)
class EvidenceRow:
    """One active ``SUPPORTS``/``REFUTES`` edge as read from AGE.

    ``source`` is the evidence (Fact/Conclusion) bearing on ``target`` (Hypothesis/Conclusion)
    — the schema direction (§5, §10). ``sign`` selects support vs attack; ``strength`` is the
    §7.1 calibrated edge weight in ``[0, 1]`` (never a raw LLM number, §8/§10).
    """

    source: ArgId
    target: ArgId
    sign: EdgeSign
    strength: Strength


@dataclass(frozen=True)
class AdjudicationInput:
    """The two inputs the QBAF engine consumes, assembled from the active graph.

    ``baf`` carries the arguments + weighted support/attack; ``base`` is each argument's Layer B
    ``confidence`` = its QBAF intrinsic score (§12 seam). Kept separate, never merged.
    """

    baf: BAF
    base: dict[ArgId, Strength] = field(default_factory=dict)


def assemble_baf(
    nodes: Iterable[NodeRow],
    edges: Iterable[EvidenceRow],
    *,
    active_box_ids: frozenset[str] | None = None,
) -> AdjudicationInput:
    """Regroup raw AGE rows into an :class:`AdjudicationInput` — the pure core of the adapter.

    DB-free, so unit-testable with hand-built rows. The steps:

    1. **Active-node universe.** A node is *active* iff ``active_box_ids`` is ``None`` (no box
       filter) or its box is in that set. Active node ids are the BAF **arguments**; their
       ``confidence`` is the **base** map (the §12 intrinsic score).
    2. **Edges.** Each :class:`EvidenceRow` whose **both** endpoints are active becomes an
       :class:`~iknos.core.qbaf.Edge` (``src=source``, ``dst=target``, ``strength``), routed by
       ``sign`` to ``supports`` / ``attacks``. An edge with an inactive endpoint is **dropped**
       (a retracted/deprecated-box supporter lends nothing — support is additive).

    Ordering is deterministic (sorted) so the produced framework and any replay trace are stable
    regardless of row iteration order (§10).
    """
    node_box: dict[ArgId, str | None] = {}
    node_conf: dict[ArgId, Strength] = {}
    for row in nodes:
        node_box[row.id] = row.box
        node_conf[row.id] = row.confidence

    def is_active(nid: ArgId) -> bool:
        if nid not in node_box:
            return False
        return active_box_ids is None or node_box[nid] in active_box_ids

    arguments = frozenset(nid for nid in node_box if is_active(nid))
    base = {nid: node_conf[nid] for nid in arguments}

    supports: list[Edge] = []
    attacks: list[Edge] = []
    for e in edges:
        if not (is_active(e.source) and is_active(e.target)):
            continue
        edge = Edge(src=e.source, dst=e.target, strength=e.strength)
        (supports if e.sign is EdgeSign.SUPPORTS else attacks).append(edge)

    def edge_key(e: Edge) -> tuple[ArgId, ArgId, Strength]:
        return (e.src, e.dst, e.strength)

    baf = BAF(
        arguments=arguments,
        supports=tuple(sorted(supports, key=edge_key)),
        attacks=tuple(sorted(attacks, key=edge_key)),
    )
    return AdjudicationInput(baf=baf, base=base)


@dataclass(frozen=True)
class HypothesisVerdict:
    """The computed adjudication of one ``Hypothesis`` (§7.2, §10, §11.2) — never hand-set.

    ``acceptability`` is the real-valued QBAF strength; ``band`` is its §11.2 presentation
    verdict (derived, not stored); ``state`` is the discrete supported/unsupported/refuted
    outcome. ``band`` is carried here for the caller's convenience but is *not* persisted.
    """

    id: ArgId
    acceptability: Strength
    band: AcceptabilityBand
    state: HypothesisState


@dataclass(frozen=True)
class AdjudicationResult:
    """The outcome of :func:`adjudicate`: every argument's acceptability + the per-Hypothesis
    verdicts, plus the QBAF convergence status (§13).

    ``acceptability`` covers all arguments (a Fact/Conclusion has a computed acceptability too,
    though only a ``Hypothesis`` gets a ``state``/``band``). ``unstable`` is non-empty only when
    the QBAF did not converge — the unresolved region to surface as a finding (§7.2, §13).
    """

    acceptability: dict[ArgId, Strength]
    verdicts: tuple[HypothesisVerdict, ...]
    converged: bool
    unstable: frozenset[ArgId] = field(default_factory=frozenset)

    @property
    def is_finding(self) -> bool:
        """Whether the QBAF left an unresolved region (did not converge) — §13."""
        return not self.converged


def adjudicate(
    inp: AdjudicationInput,
    hypothesis_ids: Iterable[ArgId],
    *,
    semantics: GradualSemantics = DEFAULT_SEMANTICS,
) -> AdjudicationResult:
    """Run the QBAF over a loaded subgraph and read off per-``Hypothesis`` verdicts (pure; no DB).

    ``solve`` → ``aggregate_evidence`` → ``band`` / ``classify_state``, computing a
    :class:`HypothesisVerdict` for exactly the ``hypothesis_ids`` present in the active subgraph
    (``state``/``band`` are ``Hypothesis`` concepts, §11.2; Facts/Conclusions carry only a
    Layer B confidence). A hypothesis id absent from the active subgraph is skipped (nothing to
    adjudicate). Determinic order (sorted ids).
    """
    result = solve(inp.baf, base=inp.base, semantics=semantics)
    aggregates = aggregate_evidence(inp.baf, result.acceptability, semantics=semantics)

    verdicts: list[HypothesisVerdict] = []
    for hid in sorted(set(hypothesis_ids)):
        if hid not in result.acceptability:
            continue
        acceptability = result.acceptability[hid]
        agg_support, agg_attack = aggregates[hid]
        verdicts.append(
            HypothesisVerdict(
                id=hid,
                acceptability=acceptability,
                band=band(acceptability),
                state=classify_state(
                    acceptability=acceptability,
                    aggregate_support=agg_support,
                    aggregate_attack=agg_attack,
                ),
            )
        )
    return AdjudicationResult(
        acceptability=dict(result.acceptability),
        verdicts=tuple(verdicts),
        converged=result.converged,
        unstable=result.unstable,
    )


async def load_evidential_edges(session: object) -> list[EvidenceRow]:
    """All current ``SUPPORTS``/``REFUTES`` edges between current nodes, with sign+strength.

    One query per relationship type (AGE matches a single label per pattern, as in
    ``load_reasoning_nodes``); the ``sign`` comes from which type matched (the canonical source of
    direction), so the edge's stored ``sign`` property need not be re-read. Shared module-level read
    (like ``load_reasoning_nodes`` / ``load_hypothesis_ids``) so the revision loop (W1) consumes the
    *same* evidential-edge definition the adapter does — the active subgraph cannot diverge.
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    rows: list[EvidenceRow] = []
    for rel, sign in (("SUPPORTS", EdgeSign.SUPPORTS), ("REFUTES", EdgeSign.REFUTES)):
        raw = await execute_cypher(
            session,  # type: ignore[arg-type]
            f"MATCH (s)-[r:{rel}]->(t) "
            "WHERE s.valid_to IS NULL AND t.valid_to IS NULL AND r.valid_to IS NULL "
            "RETURN s.id, t.id, r.strength",
            returns="sid agtype, tid agtype, strength agtype",
        )
        for sid, tid, strength in raw:
            rows.append(
                EvidenceRow(
                    source=unquote_agtype(sid),
                    target=unquote_agtype(tid),
                    sign=sign,
                    strength=_num(strength, default=1.0),
                )
            )
    return rows


# The reason string stamped on a held refutation — the §13 finding handle a reader greps for.
PENDING_REFUTATION_REASON = "ensemble_gate_pending"


def refutation_held(state: HypothesisState, decision: GateDecision | None) -> bool:
    """Whether a computed verdict's flip to ``refuted`` must be **held** (§7.2, V8) — pure.

    ``True`` iff the computed ``state`` is ``REFUTED`` **and** there is no *authorising*
    :class:`~iknos.core.ensemble_gate.GateDecision` (``decision`` is ``None`` — no ensemble ran —
    or it withheld). A held flip keeps the hypothesis's prior state + flags ``pending_refutation``;
    an authorising decision lets the ``refuted`` write through, and a non-refuted computed state is
    never held. This is the structural §7.2 invariant: ``refuted`` is unreachable without an
    authorising decision. ``decision is None`` is treated as *not authorised* (the gate did not
    speak), never as a pass — the caller withholds the flip rather than defaulting it open.
    """
    if state is not HypothesisState.REFUTED:
        return False
    return decision is None or not decision.authorised


@dataclass(frozen=True)
class HeldRefutation:
    """One structural ``refuted`` the ensemble would not authorise — held, not flipped (§7.2/§13).

    ``held_state`` is the prior state the hypothesis was kept at (``UNSUPPORTED`` when it had none);
    ``decision`` is the withholding :class:`~iknos.core.ensemble_gate.GateDecision` (``None`` when
    no ensemble ran at all). Surfaced as a §13 finding the caller routes for expert review — the
    unresolved region, never silently smoothed into a verdict.
    """

    id: ArgId
    held_state: HypothesisState
    decision: GateDecision | None = None
    reason: str = PENDING_REFUTATION_REASON


@dataclass(frozen=True)
class PersistResult:
    """The outcome of :meth:`QbafAdapter.persist_verdicts` (§7.2, §10, §13).

    ``written`` is the number of ``Hypothesis`` nodes updated; ``held`` the refutations withheld by
    the gate (each carrying its held state + the withholding decision). A non-empty ``held`` is a
    §13 finding — surface it, do not treat the held hypotheses as decided.
    """

    written: int = 0
    held: tuple[HeldRefutation, ...] = ()

    @property
    def is_finding(self) -> bool:
        """Whether any flip was withheld — an unresolved region to surface (§7.2, §13)."""
        return bool(self.held)


class QbafAdapter:
    """Loads the active evidential subgraph from AGE, adjudicates, and writes verdicts back.

    DB-free to construct; the reads/writes happen in the ``async`` methods. Stateless across
    calls — each evaluation is a full current-state read (incremental maintenance is the §13
    deferred path). Mirrors :class:`~iknos.core.derivation_adapter.DerivationGraphAdapter`'s
    boundary discipline (lazy ``iknos.db.age`` import; pure assembly in :func:`assemble_baf`,
    pure evaluation in :func:`adjudicate`).
    """

    async def _load_hypothesis_ids(self, session: object) -> set[ArgId]:
        """The ids of current ``Hypothesis`` nodes — the args that get a ``state``/verdict.

        Delegates to the shared :func:`~iknos.core.derivation_adapter.load_hypothesis_ids` so the
        "current Hypothesis" definition stays single-sourced across adjudication (here) and
        candidate generation (``core/candidates.py``).
        """
        return await load_hypothesis_ids(session)

    async def _load_evidential_edges(self, session: object) -> list[EvidenceRow]:
        """Deprecated alias — delegates to the shared :func:`load_evidential_edges`."""
        return await load_evidential_edges(session)

    async def load_inputs(self, session: object) -> AdjudicationInput:
        """Read the active evidential subgraph and assemble the QBAF inputs (BAF + base map)."""
        active_box_ids = await load_active_box_ids(session)
        nodes = await load_reasoning_nodes(session)
        edges = await load_evidential_edges(session)
        return assemble_baf(nodes, edges, active_box_ids=active_box_ids)

    async def evaluate(
        self,
        session: object,
        *,
        semantics: GradualSemantics = DEFAULT_SEMANTICS,
    ) -> AdjudicationResult:
        """Load the active subgraph and adjudicate it (read-and-evaluate; no write).

        The Phase-4 analogue of ``derivation_adapter.support_and_confidence`` — the read path
        the integration test and the composed-loop body (G4.5) exercise.
        """
        inputs = await self.load_inputs(session)
        hypothesis_ids = await self._load_hypothesis_ids(session)
        return adjudicate(inputs, hypothesis_ids, semantics=semantics)

    async def persist_verdicts(
        self,
        session: object,
        verdicts: Iterable[HypothesisVerdict],
        *,
        gate_decisions: Mapping[ArgId, GateDecision] | None = None,
    ) -> PersistResult:
        """Write each verdict's ``acceptability`` + ensemble-gated ``state`` to its ``Hypothesis``.

        A **partial** ``SET h.acceptability=…, h.state=…`` (not ``merge_vertex``'s full
        ``SET n = {…}``, which would clobber the node's bitemporal/confidence fields). The
        presentation ``band`` is deliberately **not** stored (§11.2: computed at render time).

        **The §7.2 gate is structural here (V8).** ``gate_decisions`` maps a hypothesis id to its
        :class:`~iknos.core.ensemble_gate.GateDecision` (from ``ensemble_gate.authorise``). For a
        verdict whose computed ``state`` is ``refuted`` (:func:`refutation_held`):

        - **authorising** decision → persist ``refuted`` as computed, and clear the pending flag;
        - **no decision / withheld** → **hold** the flip: persist the computed ``acceptability`` but
          keep ``state`` at the hypothesis's **prior** value (read first; ``UNSUPPORTED`` if it had
          none) and set ``pending_refutation = true`` — surfaced on :class:`PersistResult` as a §13
          finding (``ensemble_gate_pending``), never silently flipped or dropped.

        A **non-refuted** verdict is persisted as computed and **clears** ``pending_refutation`` (a
        later run that finds the hypothesis no longer refuted, or now authorised, lifts a prior
        hold). So ``refuted`` is unreachable through this writer without an authorising decision —
        ``ensemble_gate.authorise`` is the only intended producer of one. Returns the count written
        plus the held refutations.
        """
        from iknos.db.age import execute_cypher

        decisions = gate_decisions or {}
        written = 0
        held: list[HeldRefutation] = []
        for v in verdicts:
            decision = decisions.get(v.id)
            if refutation_held(v.state, decision):
                # Read the prior state first, then hold there (none → UNSUPPORTED). Read-then-write
                # in the caller's transaction — no other writer touches Hypothesis.state, and this
                # avoids relying on a coalesce-in-SET that AGE may not implement.
                prior = await self._load_state(session, v.id)
                held_state = prior if prior is not None else HypothesisState.UNSUPPORTED
                await execute_cypher(
                    session,  # type: ignore[arg-type]
                    f"MATCH (h:Hypothesis {{id: '{v.id}'}}) WHERE h.valid_to IS NULL "
                    f"SET h.acceptability = {float(v.acceptability)}, "
                    f"h.state = '{held_state.value}', h.pending_refutation = true",
                )
                held.append(HeldRefutation(id=v.id, held_state=held_state, decision=decision))
            else:
                await execute_cypher(
                    session,  # type: ignore[arg-type]
                    f"MATCH (h:Hypothesis {{id: '{v.id}'}}) WHERE h.valid_to IS NULL "
                    f"SET h.acceptability = {float(v.acceptability)}, h.state = '{v.state.value}', "
                    f"h.pending_refutation = false",
                )
            written += 1
        return PersistResult(written=written, held=tuple(held))

    async def _load_state(self, session: object, hypothesis_id: ArgId) -> HypothesisState | None:
        """The hypothesis's current ``state`` (``None`` if unset/absent) — the held-flip prior.

        A single current-row read so a withheld ``refuted`` keeps the prior state rather than
        inventing one; an unrecognised value (a graph written under a newer vocabulary) reads as
        ``None`` → caller defaults to ``UNSUPPORTED``, never aborting on a metadata surprise.
        """
        from iknos.db.age import execute_cypher

        rows = await execute_cypher(
            session,  # type: ignore[arg-type]
            f"MATCH (h:Hypothesis {{id: '{hypothesis_id}'}}) WHERE h.valid_to IS NULL "
            "RETURN h.state",
            returns="state agtype",
        )
        if not rows:
            return None
        raw = rows[0][0]
        if raw is None or str(raw) == "null":
            return None
        from iknos.db.age import unquote_agtype

        try:
            return HypothesisState(unquote_agtype(raw))
        except ValueError:
            return None


def _num(v: object, *, default: float) -> float:
    """Parse an agtype number that may be SQL/agtype null into ``float`` (mirrors the
    derivation adapter's null-tolerant parse; edge ``strength`` is required by the schema, so
    the default only guards a malformed row)."""
    if v is None or str(v) == "null":
        return default
    return float(str(v))
