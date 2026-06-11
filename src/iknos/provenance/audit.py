"""Provenance & audit read-back (Phase 2 G2.7; architecture.md §10.1, §10.2).

Phase 2's operators each write their provenance edges and an :class:`~iknos.db.orm.Action`
*at creation* (the ``extract`` operator writes ``EVIDENCED_BY`` Fact→Proposition/Span and
an ``extract`` Action; the box registry, resolver, reference binder, and part-whole inducer
each log their own). This module is the **read side** that turns that into the §10.2
auditability *guarantee* — and makes it checkable:

- :func:`fact_provenance` — the per-Fact reach-back: from a Fact, reach its source Span(s),
  the source **text** each span quotes, its evidencing Proposition, and the **producing
  Action**. This is what an expert (Phase 7) or a debugger follows to answer "where did
  this Fact come from?".
- :func:`audit_box_facts` — the box-level invariant behind the Phase 2 exit criterion
  ("no node exists without provenance and an Action record"): the Facts in a box that fail
  the auditability check, with *why* they fail. An empty list means the box is fully
  auditable.

Read-only: no writes, no transaction ownership, so it is safe to call mid-pipeline or in a
read-only session. ``iknos.db.age`` is imported lazily inside the functions (the
module-import-stays-DB-free discipline shared with ``core/extract.py`` and the box registry),
so importing this module — hence unit-testing :func:`provenance_gaps` — never pulls in the
``DATABASE_URL`` config singleton.

Scope (documented seams): the reach-back is **Fact-anchored** — Facts are the §10.2 example
and the only nodes a single Action's ``outputs.fact`` keys directly. The actors/objects a
Fact involves are recorded in the *same* extract Action (``outputs.actors``/``objects``) and
reached via the Fact, so they inherit its auditability; a *universal* per-node/edge crawler
(every Actor/Object/edge proven independently) is a later, heavier check and is not built
here.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.extract import EXTRACTOR_ACTOR
from iknos.db.spans import resolve_span_text

# --- auditability gap vocabulary (§10.2) ---------------------------------------------
# The three ways a Fact can fail the reach-back, as stable strings so callers (and tests)
# can assert on specific gaps rather than a boolean.
MISSING_SPAN = "no-evidence-span"  # no EVIDENCED_BY path to any Span
MISSING_SOURCE_TEXT = "no-resolvable-source-text"  # reaches a Span, but no span's text resolves
MISSING_ACTION = "no-producing-action"  # no extract Action names this Fact in its outputs


@dataclass(frozen=True)
class SpanRef:
    """A source Span a Fact is evidenced by, with the source text it quotes (§10.2).

    ``text`` is the resolved substring of the source document at ``[start, end)``, or
    ``None`` when it could not be resolved (e.g. the ``document_content`` row is missing) —
    the distinction the §10.2 check turns on: reaching the span node is not enough, the
    source *text* must be reachable.
    """

    span_id: uuid.UUID
    document_id: uuid.UUID
    start: int
    end: int
    text: str | None


@dataclass(frozen=True)
class ProducingAction:
    """The :class:`~iknos.db.orm.Action` that produced a Fact (§10.1) — its audit identity.

    A thin projection of the Action row (id/actor/type/model): enough to identify the
    producing run and join back to the full row, without copying the whole inputs/outputs
    payload into every reach-back.
    """

    id: uuid.UUID
    actor: str
    action_type: str
    model: str | None


@dataclass(frozen=True)
class FactProvenance:
    """The full §10.2 reach-back for one Fact: its proposition, spans+text, producing Action."""

    fact_id: uuid.UUID
    proposition_id: uuid.UUID | None
    spans: list[SpanRef]
    action: ProducingAction | None

    @property
    def is_auditable(self) -> bool:
        """True iff the §10.2 guarantee holds for this Fact (no provenance gaps)."""
        return not provenance_gaps(self)


@dataclass(frozen=True)
class AuditViolation:
    """A Fact that fails the auditability invariant, and the set of gaps explaining why."""

    fact_id: uuid.UUID
    gaps: frozenset[str]


def provenance_gaps(prov: FactProvenance) -> frozenset[str]:
    """The auditability gaps for a Fact's provenance (§10.2) — empty iff fully auditable.

    Pure (no I/O), so the invariant is unit-tested without a graph. A missing span subsumes
    missing source text (a Fact with no Span is not *also* reported as missing text — that
    would be redundant); reaching *any* span whose text resolves satisfies the source-text
    requirement.
    """
    gaps: set[str] = set()
    if not prov.spans:
        gaps.add(MISSING_SPAN)
    elif not any(s.text is not None for s in prov.spans):
        gaps.add(MISSING_SOURCE_TEXT)
    if prov.action is None:
        gaps.add(MISSING_ACTION)
    return frozenset(gaps)


async def fact_provenance(session: AsyncSession, fact_id: uuid.UUID) -> FactProvenance | None:
    """Walk a Fact's full provenance (§10.2), or ``None`` if no such Fact exists.

    Three reads: (1) the Fact + its evidencing Proposition (one ``OPTIONAL MATCH`` that also
    serves as the existence probe — zero rows means no Fact); (2) the evidencing Spans, each
    resolved to its source text via :func:`~iknos.db.spans.resolve_span_text`; (3) the
    producing ``extract`` Action, newest-first (backed by the partial functional index from
    migration 0009). Read-only.
    """
    from iknos.db.age import execute_cypher, parse_agtype_map, unquote_agtype

    # (1) Existence + evidencing Proposition. OPTIONAL MATCH so a Fact with no Proposition
    # still returns one row (with a null pid); zero rows means the Fact does not exist.
    prop_rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {{id: '{fact_id}'}}) "
        f"OPTIONAL MATCH (f)-[:EVIDENCED_BY]->(p:Proposition) RETURN p.id",
        returns="pid agtype",
    )
    if not prop_rows:
        return None
    proposition_id: uuid.UUID | None = None
    for (pid,) in prop_rows:
        # A null OPTIONAL MATCH scalar comes back as Python None or the agtype string
        # "null" — both mean "no Proposition" (the codebase idiom, cf. reference.py).
        if pid is not None and str(pid) != "null":
            proposition_id = uuid.UUID(unquote_agtype(pid))
            break

    # (2) Evidencing Spans → source text. Read the whole property map so the contract stays
    # in one place (parse_agtype_map), not field-by-field RETURNs that could drift.
    span_rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {{id: '{fact_id}'}})-[:EVIDENCED_BY]->(s:Span) RETURN properties(s)",
        returns="props agtype",
    )
    spans: list[SpanRef] = []
    for (props_raw,) in span_rows:
        props = parse_agtype_map(props_raw)
        document_id = uuid.UUID(str(props["document_id"]))
        start = int(props["start"])
        end = int(props["end"])
        spans.append(
            SpanRef(
                span_id=uuid.UUID(str(props["id"])),
                document_id=document_id,
                start=start,
                end=end,
                text=await resolve_span_text(session, document_id, start, end),
            )
        )

    # (3) Producing Action: the extract Action naming this Fact in its outputs (§10.1).
    return FactProvenance(
        fact_id=fact_id,
        proposition_id=proposition_id,
        spans=spans,
        action=await producing_action(session, fact_id),
    )


async def producing_action(session: AsyncSession, fact_id: uuid.UUID) -> ProducingAction | None:
    """The ``extract`` Action that produced ``fact_id`` (§10.1), newest-first, or ``None``.

    Filters on ``actor = 'extractor'`` and ``outputs->>'fact'`` — the exact predicate the
    migration-0009 partial functional index serves, so this stays O(log n) as the audit log
    grows unbounded (cf. the G1.7 idempotency index).
    """
    row = (
        await session.execute(
            text(
                "SELECT id, actor, action_type, model FROM actions "
                "WHERE actor = :actor AND outputs->>'fact' = :fid "
                "ORDER BY timestamp DESC LIMIT 1"
            ),
            {"actor": EXTRACTOR_ACTOR, "fid": str(fact_id)},
        )
    ).first()
    if row is None:
        return None
    return ProducingAction(id=row.id, actor=row.actor, action_type=row.action_type, model=row.model)


async def audit_box_facts(session: AsyncSession, box_id: uuid.UUID) -> list[AuditViolation]:
    """The Facts in ``box_id`` that fail the §10.2 auditability check — the exit-criterion probe.

    Returns one :class:`AuditViolation` per non-auditable Fact (with the gap set); an **empty
    list means every Fact in the box is fully auditable**. Lists the box's Facts, then runs
    :func:`fact_provenance` per Fact — fine for an audit/exit-criterion sweep (not a hot
    path); a set-based bulk variant is a later optimization if box sizes demand it.
    """
    from iknos.db.age import execute_cypher, unquote_agtype

    fact_rows = await execute_cypher(
        session,
        f"MATCH (f:Fact {{box: '{box_id}'}}) RETURN f.id",
        returns="fid agtype",
    )
    violations: list[AuditViolation] = []
    for (fid_raw,) in fact_rows:
        fact_id = uuid.UUID(unquote_agtype(fid_raw))
        prov = await fact_provenance(session, fact_id)
        # prov is non-None (we just listed the Fact); the guard keeps a concurrently-deleted
        # Fact from raising rather than reporting it as a violation.
        gaps = provenance_gaps(prov) if prov is not None else frozenset({MISSING_ACTION})
        if gaps:
            violations.append(AuditViolation(fact_id=fact_id, gaps=gaps))
    return violations
