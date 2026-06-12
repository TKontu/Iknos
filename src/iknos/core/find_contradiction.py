"""The §12 ``find-contradiction`` operator — the targeted refuter pass + the §7.2 gate (G4.5).

The last G4.5 operator, and the one the whole non-monotonic layer exists to discipline: for a
hypothesis ``h``, drive the existing candidate funnel + edge judge toward a **refutation**, and —
only if the ensemble gate authorises it — feed the retraction into the §12 belief-revision loop.
It is **composition, not new machinery** (the shape ``corroborate`` set, PR #110): the value is the
named §12 entry point that *safely* wires ``REFUTES → gate → retract → A → B → QBAF`` together,
with the gate's §7.2 invariant ("refuted is unreachable except through the gate") structural at
every seam.

**The pipeline (architecture §7.2, §5.1, §12).**

1. **Targeted refuter pass.** Reuse :meth:`~iknos.core.edge_producer.EdgeProducer.corroborate`
   (the §12 gather, atomic: edges + per-hypothesis edge-judge Action + the corroborate envelope all
   commit together) to judge ``h``'s candidate evidence and persist the surviving
   ``SUPPORTS``/``REFUTES`` edges. The judge is **blind** (§8) — we never bias it toward refutation;
   the "targeting" is that this operator *acts on the refuters it surfaces*, not that it tilts the
   panel. No persisted refuter ⇒ nothing to contradict (an auditable "looked, found no refutation").

2. **The §7.2 ensemble gate** (:func:`~iknos.core.ensemble_gate.authorise_from_panel`), assembled
   per the three channels:

   - **LLM** — :func:`~iknos.core.ensemble_gate.llm_channel` over the **persisted** edges (the
     post-quarantine :class:`~iknos.core.edge_producer.ProducedEdge`s, which carry the
     ``sign``/``sign_stable``/``hypothesis`` surface ``llm_channel`` reads): a stable ``REFUTES``
     ⇒ AFFIRM, a sign-unstable / absent refuter ⇒ ABSTAIN. Feeding the *persisted* edges (not the
     raw panel) is deliberate — a §3.1-quarantined refuter was dropped from the plan and must
     **not**
     count toward authorising a flip (the safe reading; the panel would over-count).
   - **SYMBOLIC** — :func:`~iknos.core.symbolic_gate.symbolic_channel_for` over a
     :class:`~iknos.core.symbolic_gate.SymbolicQuery` this operator **builds from the active
     sub-region** (:meth:`FindContradiction.build_symbolic_query`): the architecture's named
     consuming seam — it
     assigns each proposition the embedding twin-cluster claim key (so a claim and its negation
     share
     a key) and its :class:`~iknos.types.epistemic.Polarity`, then asks clingo whether asserting
     ``h`` and the refuter together is UNSAT. A real ``P ∧ ¬P`` ⇒ AFFIRM; merely-contrary evidence
     that shares no claim atom ⇒ ABSTAIN (so the flip is *held* for expert review — the correct
     §7.2 conservatism). When ``h`` has no embeddable claim the sub-region cannot be built and the
     channel falls back to the ABSTAIN seam.
   - **TEMPORAL** — the ABSTAIN seam (:func:`~iknos.core.ensemble_gate.temporal_channel`); its
     §7.4 bitemporal producer is a later slice (deliberately deferred, not built here).

   Under :data:`~iknos.core.ensemble_gate.DEFAULT_GATE` (``{LLM, SYMBOLIC}`` required, any DISSENT
   vetoes) the flip is authorised **iff** the panel produced a stable refuter **and** the symbolic
   check confirms a genuine logical contradiction — anything less is **held**.

3. **The revision loop** (:meth:`~iknos.core.revision_loop.RevisionLoop.run`). The gate decision is
   injected as the loop's ``decide`` for ``h``'s structurally-``refuted`` verdict; the **default**
   reviser (:func:`~iknos.core.revision_loop.retract_authorised_refuted`) retracts an
   *authorised*-refuted ``h`` so its dependents re-ground, and the pure
   :func:`~iknos.core.composed_loop.stabilize` driver runs it to a fixpoint. **``stabilize`` is the
   only loop driver — this operator writes no ad-hoc loop** (the grep-able acceptance). An
   *un-authorised* structural flip is **held** by the loop's V8 ``persist_verdicts`` filter at
   ``h``'s
   prior state + ``pending_refutation`` — the §13 finding, surfaced never written.

**The operator Action envelope (§10.1).** A ``find-contradiction`` Action wraps the run with full
provenance — the gathered refuters/supporters, the corroborate envelope it drove, the per-channel
gate breakdown (and *why* a flip was withheld), the loop's iteration Actions, and what was retracted
— so an operator run is never invisible and the gate decision is auditable.

**Re-run / idempotency (the append-only provenance contract).** Re-running ``find-contradiction`` on
``h`` re-pays the LLM judge and re-runs the gate + loop from current state; the edges are
``merge_edge``-deduped (same ``(src, dst, label)`` MERGE), retractions are idempotent (a node's
``valid_to`` once stamped stays stamped — ``stabilize``'s monotone retracted set never un-retracts),
and every Action (edge-judge, corroborate, the loop iterations, this envelope) **appends** per run —
accepted append-only provenance, not a mutation. A second run after an authorised retraction finds
``h`` no longer current (no candidates) and records the auditable "looked, found nothing"; a run
after a *held* flip may now authorise if the symbolic sub-region has gained the missing claim.

**Pure / DB split (the codebase discipline).** The claim-key clustering, atom assembly and
sub-region → :class:`~iknos.core.symbolic_gate.SymbolicQuery` build are DB-free and unit-testable
with hand-built rows (:func:`assign_claim_keys`, :func:`assemble_symbolic_query`);
:meth:`FindContradiction.build_symbolic_query` and :class:`FindContradiction` do the AGE / pgvector
reads behind a lazy import.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from iknos.core.candidates import DEFAULT_K
from iknos.core.consistency import DEFAULT_AGREEMENT_THRESHOLD
from iknos.core.edge_producer import CorroborateResult, EdgeProducer
from iknos.core.ensemble_gate import (
    DEFAULT_GATE,
    GateDecision,
    RefutationGate,
    authorise_from_panel,
)
from iknos.core.qbaf_adapter import HypothesisVerdict
from iknos.core.revision_loop import RevisionLoop, RevisionResult
from iknos.core.symbolic_gate import Atom, SymbolicQuery, symbolic_channel_for
from iknos.core.truth_maintenance import NodeId
from iknos.types.epistemic import Polarity
from iknos.types.intentional import HypothesisState

logger = logging.getLogger(__name__)

# The operator's own Action (§12 named entry point) — an envelope over the corroborate Action and
# the revision-loop Actions, never a replacement, so the lower-level provenance stays greppable.
FIND_CONTRADICTION_ACTOR = "find-contradiction"
FIND_CONTRADICTION_ACTION_TYPE = "find-contradiction"


@dataclass(frozen=True)
class SubregionProposition:
    """One ``EVIDENCED_BY`` proposition in the symbolic sub-region — the atom-building input.

    ``node`` is the reasoning node the claim belongs to (the hypothesis or a refuter/supporter),
    ``proposition`` the source ``Proposition`` id, ``polarity`` its asserted/negated sign (§3.1,
    G1.1), and ``(model, vector)`` its ``proposition_embeddings`` row — the **vector-space
    identity**
    (G1.16): two claims are only ever co-clustered within the same ``model``. A proposition with no
    embedding row is dropped upstream (it cannot be placed in claim-space), and the **first**
    model's
    vector is taken when a proposition straddles models mid-migration (deterministic, single space).
    """

    node: NodeId
    proposition: NodeId
    polarity: Polarity
    text: str
    model: str
    vector: tuple[float, ...]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity (a plain dot product on L2-normalized bge-m3 vectors; norms divided out
    defensively so a zero/degenerate vector reads 0.0, never a divide-by-zero). Local to keep the
    sub-region adapter self-contained, mirroring ``candidates._cosine_similarity`` /
    ``consistency._cosine`` (W8 is where the cosine helpers consolidate)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(dot / (na * nb))


def assign_claim_keys(
    props: Sequence[SubregionProposition], *, threshold: float = DEFAULT_AGREEMENT_THRESHOLD
) -> dict[NodeId, str]:
    """Assign each proposition its **claim-identity key** — the embedding twin-cluster id (pure).

    The key is what makes a ``P`` / ``¬P`` clash detectable: a claim and its *negation* sit at
    cosine > 0.9 (sentence embeddings cannot tell a claim from its negation, G1.14), so clustering
    **polarity-blind** lands the asserted and negated phrasings of one claim in the **same** cluster
    — the same key — while their :class:`~iknos.types.epistemic.Polarity` (carried on the
    :class:`~iknos.core.symbolic_gate.Atom`) keeps them opposite literals. The symbolic engine then
    sees ``holds(K, t)`` and ``holds(K, f)`` and its integrity constraint fires (the §8(d) check).

    Deterministic greedy-against-representative clustering (the
    :func:`~iknos.core.consistency.cluster_candidates` discipline) in sorted-proposition-id order,
    run
    **within each embedding model** (the G1.16 guard — cosine across models is meaningless, so
    cross-
    model claims get distinct keys and can never clash): the key is ``"{model}#{cluster_index}"``.
    """
    by_model: dict[str, list[SubregionProposition]] = {}
    for p in props:
        by_model.setdefault(p.model, []).append(p)

    keys: dict[NodeId, str] = {}
    for model in sorted(by_model):
        reps: list[tuple[float, ...]] = []  # each cluster's opening representative vector
        for p in sorted(by_model[model], key=lambda p: p.proposition):
            for idx, rep in enumerate(reps):
                if _cosine(p.vector, rep) >= threshold:
                    keys[p.proposition] = f"{model}#{idx}"
                    break
            else:
                keys[p.proposition] = f"{model}#{len(reps)}"
                reps.append(p.vector)
    return keys


def _atoms_for(
    nodes: Sequence[NodeId],
    by_node: Mapping[NodeId, list[SubregionProposition]],
    keys: Mapping[NodeId, str],
) -> tuple[Atom, ...]:
    """The atoms a set of nodes assert: one :class:`Atom` per proposition (key + polarity) — pure.

    Deterministically ordered by ``(node, proposition)`` so a replay (and the ASP grounding) is
    stable (§10). An ``ASSERTED`` proposition is a positive literal, a ``NEGATED`` one its twin.
    """
    atoms: list[Atom] = []
    for node in sorted(set(nodes)):
        for p in sorted(by_node.get(node, []), key=lambda p: p.proposition):
            atoms.append(Atom(key=keys[p.proposition], positive=p.polarity is Polarity.ASSERTED))
    return tuple(atoms)


def assemble_symbolic_query(
    *,
    hypothesis_id: NodeId,
    refuter_ids: Sequence[NodeId],
    supporter_ids: Sequence[NodeId],
    props: Sequence[SubregionProposition],
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
) -> SymbolicQuery | None:
    """Build the ``(refuter → hypothesis)`` :class:`SymbolicQuery` from the sub-region props — pure.

    Assigns claim keys over **all** the sub-region's propositions (:func:`assign_claim_keys`) so the
    hypothesis, its refuters and its supporters share one claim-space, then partitions the atoms by
    role: ``hypothesis`` = ``h``'s claims, ``refuter`` = the refuting evidence's claims, ``context``
    = the supporting evidence's claims (so a *transitive* clash can route through a supporter). No
    box derivation ``rules`` in this slice — a documented seam (the two-atom polarity clash is the
    MVP; richer ASP rules slot in without a contract change, ``symbolic_gate`` §"Why clingo").

    Returns ``None`` when the hypothesis has **no** embeddable claim or no refuter does — the
    sub-region cannot be built, so the caller falls back to the SYMBOLIC ABSTAIN seam and the flip
    is
    held (the safe default: the symbolic check has nothing to say, it does not invent a dissent).
    """
    keys = assign_claim_keys(props, threshold=threshold)
    by_node: dict[NodeId, list[SubregionProposition]] = {}
    for p in props:
        by_node.setdefault(p.node, []).append(p)

    hyp_atoms = _atoms_for([hypothesis_id], by_node, keys)
    ref_atoms = _atoms_for(refuter_ids, by_node, keys)
    if not hyp_atoms or not ref_atoms:
        return None
    ctx_atoms = _atoms_for(supporter_ids, by_node, keys)
    return SymbolicQuery(hypothesis=hyp_atoms, refuter=ref_atoms, context=ctx_atoms)


@dataclass(frozen=True)
class FindContradictionResult:
    """The outcome of :meth:`FindContradiction.run` (§7.2, §12, §13).

    ``corroboration`` is the targeted refuter pass (persisted edges + the corroborate envelope
    Action); ``decision`` the §7.2 gate verdict for ``h`` (``None`` when no refuter was found, so
    the
    gate never ran); ``revision`` the loop result (``None`` when there was nothing to adjudicate);
    ``action_id`` the operator's own envelope Action. ``authorised`` / ``retracted`` /
    ``is_finding``
    are the read-offs a caller routes on.
    """

    hypothesis: NodeId
    corroboration: CorroborateResult
    decision: GateDecision | None
    revision: RevisionResult | None
    action_id: uuid.UUID

    @property
    def authorised(self) -> bool:
        """Whether the gate authorised the refuted flip (the ensemble agreed)."""
        return self.decision is not None and self.decision.authorised

    @property
    def retracted(self) -> frozenset[NodeId]:
        """The nodes the revision loop retracted on convergence (empty when nothing was
        authorised)."""
        return self.revision.retracted if self.revision is not None else frozenset()

    @property
    def is_finding(self) -> bool:
        """Whether there is an unresolved region to surface (§13).

        A **held** refutation (the gate withheld an automated flip → ``pending_refutation``, V8), a
        non-convergent revision loop, or a sign-unstable refuter the panel split on — any is the §13
        surface the caller presents for expert review, never silently smoothed.
        """
        if self.revision is not None and self.revision.is_finding:
            return True
        return self.corroboration.is_finding


class FindContradiction:
    """The §12 ``find-contradiction`` operator — composes the producer, the gate, and the loop.

    DB-free to construct (it carries an :class:`~iknos.core.edge_producer.EdgeProducer`, a
    :class:`~iknos.core.revision_loop.RevisionLoop`, and the gate policy); the reads/writes happen
    in
    :meth:`run`. Stateless across calls — a full gather → gate → stabilize per call, the operator
    discipline ``corroborate`` set.
    """

    def __init__(
        self,
        producer: EdgeProducer,
        *,
        loop: RevisionLoop | None = None,
        gate: RefutationGate = DEFAULT_GATE,
    ) -> None:
        self.producer = producer
        self.loop = loop or RevisionLoop()
        self.gate = gate

    async def run(
        self,
        session: object,
        hypothesis_id: object,
        *,
        k: int = DEFAULT_K,
    ) -> FindContradictionResult:
        """Drive the targeted refuter pass → §7.2 gate → §12 revision loop for one hypothesis.

        See the module docstring for the full pipeline. Records the operator's envelope Action and
        commits it; the corroborate pass and the revision loop each commit their own writes (so the
        gathered edges are durable before the loop reads them, and a deferred revision is a
        legitimate
        re-runnable state — the append-only provenance contract). Returns the gathered evidence, the
        gate decision, the loop outcome, and the envelope Action id.
        """
        from iknos.provenance.action_log import record_action

        target = str(hypothesis_id)
        # 1 — targeted refuter pass (atomic: edges + edge-judge Action + corroborate envelope).
        corroboration = await self.producer.corroborate(session, target, k=k)

        decision: GateDecision | None = None
        revision: RevisionResult | None = None
        if corroboration.refuters:
            # 2 — assemble the §7.2 gate channels for h. LLM from the *persisted* edges (post-
            # quarantine); SYMBOLIC from the sub-region; TEMPORAL stays the ABSTAIN seam (default).
            query = await self.build_symbolic_query(session, corroboration)
            symbolic = symbolic_channel_for(query) if query is not None else None
            gate_decision = authorise_from_panel(
                # ProducedEdge carries the EdgeJudgment surface llm_channel reads (sign/stable).
                corroboration.production.edges,
                hypothesis=target,
                symbolic=symbolic,
                gate=self.gate,
            )
            decision = gate_decision

            # 3 — feed the decision into the loop as h's gate verdict; stabilize is the only driver.
            # The decision is keyed to h for *any* structurally-refuted verdict (authorised → the
            # default reviser retracts h; withheld → V8 holds it as pending_refutation). The closure
            # binds gate_decision (a GateDecision, never None here).
            def decide(
                verdicts: Sequence[HypothesisVerdict],
            ) -> Mapping[NodeId, GateDecision]:
                return {
                    v.id: gate_decision
                    for v in verdicts
                    if v.id == target and v.state is HypothesisState.REFUTED
                }

            revision = await self.loop.run(session, decide=decide)

        action_id = await record_action(
            session,  # type: ignore[arg-type]
            actor=FIND_CONTRADICTION_ACTOR,
            action_type=FIND_CONTRADICTION_ACTION_TYPE,
            inputs={"hypothesis": target, "k": k},
            outputs=self._action_outputs(corroboration, decision, revision),
            model=getattr(self.producer.judge.llm, "model", None),
            sampling=self.producer.judge.sampling,
        )
        await session.commit()  # type: ignore[attr-defined]
        return FindContradictionResult(
            hypothesis=target,
            corroboration=corroboration,
            decision=decision,
            revision=revision,
            action_id=action_id,
        )

    def _action_outputs(
        self,
        corroboration: CorroborateResult,
        decision: GateDecision | None,
        revision: RevisionResult | None,
    ) -> dict[str, object]:
        """The envelope Action's ``outputs`` (§10.1) — the auditable account of the operator run."""
        out: dict[str, object] = {
            "refuters": [e.evidence for e in corroboration.refuters],
            "supporters": [e.evidence for e in corroboration.supporters],
            # The lower-level provenance this envelope wraps (never replaces).
            "corroborate_action": str(corroboration.action_id),
            "authorised": decision is not None and decision.authorised,
        }
        if decision is not None:
            out["gate"] = {
                "outcome": str(decision.outcome),
                "channels": [
                    {"channel": str(s.channel), "stance": str(s.stance), "detail": s.detail}
                    for s in decision.signals
                ],
                "reasons": list(decision.reasons),
            }
        if revision is not None:
            out["loop_actions"] = [str(a) for a in revision.action_ids]
            out["retracted"] = sorted(revision.retracted)
            out["finding"] = revision.is_finding
        return out

    async def build_symbolic_query(
        self, session: object, corroboration: CorroborateResult
    ) -> SymbolicQuery | None:
        """Read the active sub-region and build the SYMBOLIC channel's :class:`SymbolicQuery`.

        The architecture's named consuming seam (``symbolic_gate`` §"Pure engine, DB at the edges"):
        gathers the ``EVIDENCED_BY`` propositions (text + :class:`~iknos.types.epistemic.Polarity` +
        dense vector) of the hypothesis, its **persisted** refuters and its supporters, and hands
        the
        pure :func:`assemble_symbolic_query` the rows to cluster into claim keys and partition by
        role. ``None`` (→ the ABSTAIN seam) when the hypothesis carries no embeddable claim.
        """
        hyp = corroboration.hypothesis
        refuter_ids = [e.evidence for e in corroboration.refuters]
        supporter_ids = [e.evidence for e in corroboration.supporters]
        node_ids = {hyp, *refuter_ids, *supporter_ids}
        props = await load_subregion_propositions(session, node_ids)
        return assemble_symbolic_query(
            hypothesis_id=hyp,
            refuter_ids=refuter_ids,
            supporter_ids=supporter_ids,
            props=props,
        )


async def load_subregion_propositions(
    session: object, node_ids: set[NodeId]
) -> list[SubregionProposition]:
    """Load the ``EVIDENCED_BY`` propositions (polarity + text + dense vector) for ``node_ids``.

    The cross-store read the symbolic sub-region rides: AGE for the node → ``Proposition`` claim
    edges (polarity + text), pgvector for the dense ``proposition_embeddings`` rows. A node with no
    ``EVIDENCED_BY`` ``Proposition``, or a proposition with no embedding row, simply contributes no
    atom — it cannot be placed in claim-space (the SYMBOLIC channel then has less to see, abstaining
    where it must). One current-state scan (investigation scale, like the other adapters); the
    proposition polarity defaults to ``ASSERTED`` when unset (a pre-G1.1 claim asserts its content).
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    rows = await execute_cypher(
        session,  # type: ignore[arg-type]
        "MATCH (n)-[:EVIDENCED_BY]->(p:Proposition) WHERE n.valid_to IS NULL "
        "RETURN n.id, p.id, p.polarity, p.text",
        returns="nid agtype, pid agtype, polarity agtype, text agtype",
    )
    scoped: list[tuple[NodeId, NodeId, Polarity, str]] = []
    prop_ids: set[NodeId] = set()
    for nid_raw, pid_raw, pol_raw, text_raw in rows:
        nid = unquote_agtype(nid_raw)
        if nid not in node_ids:
            continue
        pid = unquote_agtype(pid_raw)
        scoped.append((nid, pid, _opt_polarity(pol_raw), _opt_str(text_raw) or ""))
        prop_ids.add(pid)

    vectors = await _load_proposition_vectors(session, prop_ids)
    props: list[SubregionProposition] = []
    for nid, pid, polarity, text in scoped:
        row = vectors.get(pid)
        if row is None:
            continue  # no embedding — cannot place this claim in vector space
        model, vector = row
        props.append(
            SubregionProposition(
                node=nid, proposition=pid, polarity=polarity, text=text, model=model, vector=vector
            )
        )
    return props


async def _load_proposition_vectors(
    session: object, proposition_ids: set[NodeId]
) -> dict[NodeId, tuple[str, tuple[float, ...]]]:
    """Each proposition's dense ``proposition_embeddings`` vector — the cross-store pgvector read.

    Returns, per proposition id, the **first** ``(model, vector)`` by model order — normally the
    only row; more than one only mid-model-migration, where keeping a single deterministic vector
    space per proposition (the G1.16 identity) is the honest reading (cross-model claims get
    distinct
    keys, never silently mixed). Mirrors ``candidates._load_proposition_vectors``'s read shape.
    """
    if not proposition_ids:
        return {}
    from sqlalchemy import select

    from iknos.db.orm import PropositionEmbedding

    result = await session.execute(  # type: ignore[attr-defined]
        select(
            PropositionEmbedding.proposition_id,
            PropositionEmbedding.model,
            PropositionEmbedding.embedding,
        ).where(PropositionEmbedding.proposition_id.in_([uuid.UUID(p) for p in proposition_ids]))
    )
    by_prop: dict[NodeId, list[tuple[str, tuple[float, ...]]]] = {}
    for prop_id, model, embedding in result.all():
        by_prop.setdefault(str(prop_id), []).append((model, tuple(float(x) for x in embedding)))
    return {pid: min(rows) for pid, rows in by_prop.items()}


def _opt_str(v: object) -> str | None:
    """Parse an agtype scalar that may be SQL/agtype null into ``str | None`` (the adapters'
    idiom)."""
    if v is None or str(v) == "null":
        return None
    from iknos.db.age import unquote_agtype

    return unquote_agtype(v)


def _opt_polarity(v: object) -> Polarity:
    """Parse a proposition ``polarity`` property into a :class:`~iknos.types.epistemic.Polarity`.

    Defaults to ``ASSERTED`` when unset (a pre-G1.1 claim) or unrecognised (a graph written under a
    newer vocabulary) — a claim asserts its content unless explicitly negated, never aborting a run
    on a metadata surprise. The conservative reading: an unknown polarity does not invent a
    negation.
    """
    s = _opt_str(v)
    if s is None:
        return Polarity.ASSERTED
    try:
        return Polarity(s)
    except ValueError:
        return Polarity.ASSERTED
