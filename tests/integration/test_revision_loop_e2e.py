"""W2 — the §8 small-scale experiment in test form: the composed loop end-to-end (§7.2, §12, §13).

The architecture's own *Proposed small-scale experiment* (§8) had existed in no form — every
correctness guarantee rested on per-layer unit tests. This is its **mechanical** half (the V1 gate
corpus is the *accuracy* half, real documents + gold labels): a hand-built belief graph on real
Postgres+AGE, driven through :meth:`~iknos.core.revision_loop.RevisionLoop.run` with **zero LLM
calls** — the gate decisions are injected, built through the *real*
:func:`~iknos.core.ensemble_gate.authorise` (the spec: never mock the gate). Deterministic, cheap,
and kept as a **permanent regression suite**: it goes red the moment any layer seam
(Layer A → Layer B → QBAF → gate → persist) is rewired without the others noticing.

The fixture (one active box):

* **Region A — grounded, must stay byte-stable.** A base fact ``gf`` grounds a *grounded cycle*
  ``p ↔ q`` (``p ←gf``, ``p ←q``, ``q ←p``); hypothesis ``ha`` is supported by ``p`` with no
  refuter. ``gf`` is **never** retracted, so the whole region must be untouched by surgery in
  Region B — the §12 *locality* guarantee (retraction propagates, but only along justification
  links).
* **Region B — overturned.** A base fact ``bf`` grounds an *unfounded-after-retraction cycle*
  ``x ↔ y`` (``x ←bf``, ``x ←y``, ``y ←x``); hypothesis ``hb`` is weakly supported by ``x`` and
  strongly **refuted** by an overturning fact ``r`` — so the QBAF computes ``hb`` refuted from the
  start, giving the gate a refutation to (de)authorise. The §12 revision policy for this fixture
  retracts ``bf`` (the grounding of ``hb``'s supporter) when the refutation is *authorised*; Layer A
  then tears down the now-unfounded cycle ``x, y`` while the *grounded* cycle ``p, q`` survives.

The two regions are structurally disjoint, so Region A is the oracle for "the change stayed local".
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.boxes.serde import box_to_props, case_box
from iknos.core.ensemble_gate import DEFAULT_GATE, GateChannel, affirming, authorise
from iknos.core.revision_loop import RevisionLoop, no_decisions
from iknos.db.age import (
    bootstrap_session,
    execute_cypher,
    merge_edge,
    merge_vertex,
    parse_agtype_map,
    unquote_agtype,
)
from iknos.db.orm import Action
from iknos.types.intentional import HypothesisState
from iknos.types.nodes import Box

pytestmark = pytest.mark.asyncio

# An authorising gate decision via the real authorise (DEFAULT_GATE needs {LLM, SYMBOLIC} to AFFIRM
# with no DISSENT). Built once — the loop injects it per refuted hypothesis. Never a mocked gate.
_AUTHORISED = authorise(
    [affirming(GateChannel.LLM), affirming(GateChannel.SYMBOLIC)], gate=DEFAULT_GATE
)


# --- seeding helpers (mirror the W1 integration test; one box, schemaless AGE) -----------------


async def _put_box(session: AsyncSession, box: Box) -> None:
    await merge_vertex(session, "Box", box_to_props(box))


async def _put_node(
    session: AsyncSession, label: str, box: uuid.UUID, *, confidence: float
) -> uuid.UUID:
    nid = uuid.uuid4()
    await merge_vertex(
        session,
        label,
        {"id": str(nid), "box": str(box), "confidence": confidence, "valid_to": None},
    )
    return nid


async def _evidence(session: AsyncSession, node: uuid.UUID) -> None:
    """Give a node an ``EVIDENCED_BY`` Proposition so Layer A counts it a base fact (§12)."""
    pid = uuid.uuid4()
    await merge_vertex(session, "Proposition", {"id": str(pid), "text": "claim"})
    await merge_edge(session, src_id=node, dst_id=pid, label="EVIDENCED_BY", props={})


async def _derive(
    session: AsyncSession, *, conclusion: uuid.UUID, antecedent: uuid.UUID, derivation: str
) -> None:
    """One single-antecedent rule ``conclusion ← antecedent`` with an **explicit** derivation id.

    Distinct ids matter: two rules to the same conclusion with *different* derivation ids are
    OR-support (disjunction — either grounds it), which is what makes a grounded cycle hold via its
    external base fact. (The default per-conclusion id would AND them, which we do not want here.)
    """
    await merge_edge(
        session,
        src_id=conclusion,
        dst_id=antecedent,
        label="DERIVED_FROM",
        props={"derivation": derivation, "strength": 1.0, "valid_to": None},
    )


async def _evidential(
    session: AsyncSession,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    box: uuid.UUID,
    label: str,
    strength: float,
) -> None:
    await merge_edge(
        session,
        src_id=source,
        dst_id=target,
        label=label,
        props={"box": str(box), "strength": strength, "significance": 1.0, "valid_to": None},
    )


def _norm(v: object) -> str:
    """An agtype scalar → plain string, with SQL/agtype null → ``"null"`` (cf. the W1 e2e test)."""
    if v is None or str(v) in ("null", "None"):
        return "null"
    return unquote_agtype(v)


async def _read(session: AsyncSession, nid: uuid.UUID) -> dict[str, str]:
    rows = await execute_cypher(
        session,
        f"MATCH (n {{id: '{nid}'}}) RETURN n.state, n.valid_to, n.confidence, n.pending_refutation",
        returns="state agtype, valid_to agtype, conf agtype, pending agtype",
    )
    state, valid_to, conf, pending = rows[0]
    return {
        "state": _norm(state),
        "valid_to": _norm(valid_to),
        "confidence": _norm(conf),
        "pending": _norm(pending),
    }


async def _props(session: AsyncSession, nid: uuid.UUID) -> dict[str, object]:
    """The node's full property map — the byte-stability oracle for the untouched region."""
    rows = await execute_cypher(
        session,
        f"MATCH (n {{id: '{nid}'}}) RETURN properties(n)",
        returns="props agtype",
    )
    return parse_agtype_map(rows[0][0])


async def _seed(session: AsyncSession, name: str) -> dict[str, uuid.UUID]:
    """Seed Region A (grounded cycle + ha) and Region B (unfounded cycle + hb + refuter r).

    Region A is grounded by ``gf`` (never retracted) and must stay byte-stable. Region B is grounded
    by ``bf`` (retracted when the refutation is authorised); ``hb`` is weakly supported by ``x`` and
    strongly refuted by ``r`` so it computes ``refuted`` from the start.
    """
    box = case_box(name, "1", "test", 0.8)
    await _put_box(session, box)
    b = box.id

    # Region A — grounded cycle p<->q grounded by gf; ha supported by p, no refuter.
    gf = await _put_node(session, "Fact", b, confidence=1.0)
    p = await _put_node(session, "DeductiveConclusion", b, confidence=1.0)
    q = await _put_node(session, "DeductiveConclusion", b, confidence=1.0)
    ha = await _put_node(session, "Hypothesis", b, confidence=0.5)
    await _evidence(session, gf)
    await _derive(session, conclusion=p, antecedent=gf, derivation="p<-gf")
    await _derive(session, conclusion=p, antecedent=q, derivation="p<-q")  # the cycle leg (OR)
    await _derive(session, conclusion=q, antecedent=p, derivation="q<-p")
    await _evidential(session, source=p, target=ha, box=b, label="SUPPORTS", strength=0.6)

    # Region B — unfounded-after-retraction cycle x<->y grounded by bf; hb refuted by r.
    bf = await _put_node(session, "Fact", b, confidence=1.0)
    x = await _put_node(session, "DeductiveConclusion", b, confidence=1.0)
    y = await _put_node(session, "DeductiveConclusion", b, confidence=1.0)
    hb = await _put_node(session, "Hypothesis", b, confidence=0.3)
    r = await _put_node(session, "Fact", b, confidence=1.0)
    await _evidence(session, bf)
    await _evidence(session, r)
    await _derive(session, conclusion=x, antecedent=bf, derivation="x<-bf")
    await _derive(session, conclusion=x, antecedent=y, derivation="x<-y")  # the cycle leg (OR)
    await _derive(session, conclusion=y, antecedent=x, derivation="y<-x")
    await _evidential(session, source=x, target=hb, box=b, label="SUPPORTS", strength=0.2)
    await _evidential(session, source=r, target=hb, box=b, label="REFUTES", strength=0.9)

    await session.commit()
    return {
        "box": b,
        "gf": gf, "p": p, "q": q, "ha": ha,
        "bf": bf, "x": x, "y": y, "hb": hb, "r": r,
    }  # fmt: skip


def _retract_bf_on_authorised(bf: uuid.UUID):  # noqa: ANN202
    """The §12 revision policy for this fixture: an *authorised* refutation of ``hb`` retracts the
    base fact ``bf`` grounding ``hb``'s supporter. Monotone (accumulates ``bf`` once), so the loop
    re-grounds and converges; the *default* :func:`retract_authorised_refuted` retracts the
    hypothesis instead — here we exercise the fact-retraction feedback the loop exists for (§12)."""

    def revise(verdicts, decisions, retracted):  # noqa: ANN001
        flip = any(
            v.state is HypothesisState.REFUTED and (d := decisions.get(v.id)) and d.authorised
            for v in verdicts
        )
        return (retracted | {str(bf)}) if flip else retracted

    return revise


def _authorise_refuted(verdicts):  # noqa: ANN001
    """Inject an authorising gate decision for every structurally-refuted hypothesis (W2-style:
    the channels live outside the loop; here they are pre-built through the real ``authorise``)."""
    return {v.id: _AUTHORISED for v in verdicts if v.state is HypothesisState.REFUTED}


# --- the experiment ----------------------------------------------------------------------------


async def test_overturning_fact_propagates_locally_and_flips_only_through_the_gate(
    session: AsyncSession,
) -> None:
    """The §8 experiment: a held refutation, then an authorised one that retracts a fact, drops the
    unfounded cycle, flips ``hb`` — and leaves the disjoint grounded region byte-stable (§12, §13).
    """
    await bootstrap_session(session)
    ids = await _seed(session, "w2-e2e")

    # --- Pass 1: NO gate decision. The structural refutation is *held* (V8), nothing retracted, the
    # loop converges in one pass. This both demonstrates the held half of the gate invariant and
    # settles Region A's annotations at the Layer-B fixpoint (the byte-stability baseline).
    held = await RevisionLoop().run(session, decide=no_decisions, max_iterations=10)
    assert held.converged
    assert held.retracted == frozenset()
    assert held.is_finding  # the withheld flip is surfaced (pending_refutation, §13)

    hb_held = await _read(session, ids["hb"])
    assert hb_held["state"] != "refuted"  # flip withheld — the gate is the only path
    assert hb_held["pending"] == "true"
    assert _norm((await _read(session, ids["bf"]))["valid_to"]) == "null"  # nothing retracted
    assert float((await _read(session, ids["x"]))["confidence"]) > 0.0  # cycle still grounded

    baseline = {k: await _props(session, ids[k]) for k in ("gf", "p", "q", "ha")}
    ha_held = await _read(session, ids["ha"])
    assert ha_held["state"] == "supported"  # ha unaffected by hb's held refutation

    # --- Pass 2: AUTHORISE the refutation (built through the real gate). The fixture policy
    # retracts bf; Layer A tears down the unfounded cycle x,y; hb flips; Region A is untouched.
    flipped = await RevisionLoop().run(
        session,
        decide=_authorise_refuted,
        revise=_retract_bf_on_authorised(ids["bf"]),
        max_iterations=10,
    )
    assert flipped.converged
    assert flipped.retracted == frozenset({str(ids["bf"])})
    assert flipped.stabilization.iterations >= 2  # genuinely re-ran: retract bf -> re-adjudicate
    assert (
        len(flipped.action_ids) == flipped.stabilization.iterations
    )  # one Action per step (§10.1)

    # (b) the overturned region: bf retracted, the unfounded cycle dropped, hb's flip persisted.
    bf_r = await _read(session, ids["bf"])
    assert bf_r["valid_to"] != "null"  # the supporting fact is retracted
    for node in ("x", "y"):
        nr = await _read(session, ids[node])
        assert float(nr["confidence"]) == pytest.approx(0.0)  # unfounded cycle dropped (Layer A)
    hb_r = await _read(session, ids["hb"])
    assert hb_r["state"] == "refuted" and hb_r["pending"] == "false"  # authorised flip persisted

    # (a) locality: the disjoint grounded region is byte-for-byte unchanged — the grounded cycle
    # survived and the retraction did not leak across the justification boundary.
    for k in ("gf", "p", "q", "ha"):
        assert await _props(session, ids[k]) == baseline[k], f"region A node {k} was perturbed"
    assert float((await _read(session, ids["p"]))["confidence"]) > 0.0  # grounded cycle survives

    # (e) every change is walkable through Actions (§10.2): the returned ids reconstruct the
    # retracted-set trajectory, terminal carrying the converged status.
    trail = await _action_trail(session, flipped.action_ids)
    assert [o["iteration"] for o in trail] == list(range(len(trail)))
    assert trail[-1]["retracted"] == [str(ids["bf"])]
    assert trail[-1]["status"] == "converged" and trail[-1]["converged"] is True


async def test_mutual_refutes_region_is_surfaced_not_smoothed(session: AsyncSession) -> None:
    """A crafted mutual-``REFUTES`` region under an oscillating revision policy must be **surfaced**
    as a §13 finding and commit **nothing** — never silently smoothed into a verdict (§12 principle
    8). Two hypotheses attack each other; an oscillating policy toggles a retraction forever.
    """
    await bootstrap_session(session)
    box = case_box("w2-mutual", "1", "test", 0.8)
    await _put_box(session, box)
    b = box.id
    h1 = await _put_node(session, "Hypothesis", b, confidence=0.5)
    h2 = await _put_node(session, "Hypothesis", b, confidence=0.5)
    s1 = await _put_node(session, "Fact", b, confidence=1.0)
    s2 = await _put_node(session, "Fact", b, confidence=1.0)
    await _evidence(session, s1)
    await _evidence(session, s2)
    await _evidential(session, source=s1, target=h1, box=b, label="SUPPORTS", strength=0.5)
    await _evidential(session, source=s2, target=h2, box=b, label="SUPPORTS", strength=0.5)
    await _evidential(session, source=h1, target=h2, box=b, label="REFUTES", strength=0.9)
    await _evidential(session, source=h2, target=h1, box=b, label="REFUTES", strength=0.9)
    await session.commit()

    before = {n: await _read(session, n) for n in (h1, h2, s1, s2)}

    # An oscillating §12 policy: while a refutation stands and nothing is retracted, retract s1;
    # once something is retracted, revive (drop it). The retracted set 2-cycles {} <-> {s1}, so the
    # stabilize driver must detect the cycle and surface it — never loop on it (§13).
    def toggling(verdicts, decisions, retracted):  # noqa: ANN001
        if retracted:
            return frozenset()
        return frozenset({str(s1)})

    result = await RevisionLoop().run(
        session, decide=_authorise_refuted, revise=toggling, max_iterations=20
    )

    assert not result.converged
    assert result.is_finding
    assert result.stabilization.unstable_region()  # the cycle/trajectory is surfaced, not smoothed
    assert result.persisted is None  # a non-converged loop commits no verdicts/retractions

    # Nothing was written to the graph — the mutual-refutes region is byte-stable, no false verdict.
    for n in (h1, h2, s1, s2):
        assert await _read(session, n) == before[n]

    # The finding is still auditable (§10.2): the terminal Action carries the unstable region.
    trail = await _action_trail(session, result.action_ids)
    assert trail[-1]["status"] in ("oscillating", "diverged")
    assert trail[-1]["unstable_region"]  # the §13 region recorded for the investigator


async def _action_trail(
    session: AsyncSession, action_ids: tuple[uuid.UUID, ...]
) -> list[dict[str, object]]:
    """The revision loop's per-iteration Action outputs, ordered by iteration (§10.1/§10.2).

    Fetched by the exact ids :meth:`RevisionLoop.run` returned, so two runs in one test never mix;
    ``timestamp`` is the transaction clock (identical within a run), hence the in-Python sort on the
    recorded ``iteration``.
    """
    rows = (
        (await session.execute(select(Action).where(Action.id.in_(list(action_ids)))))
        .scalars()
        .all()
    )
    return sorted((r.outputs for r in rows), key=lambda o: o["iteration"])
