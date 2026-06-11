"""The reference-binding subsystem (Phase 2, G2.4; architecture.md §3.1, §6, §10).

A proposition's surface references — a pronoun ("it"), a definite description ("the
bearing"), a named reference ("bearing 3") — denote entities that already live in the
graph. **Reference binding is a separate, scored decision, not resolved invisibly** (§3.1):
*detecting* that a mention needs a referent is robust, but choosing *which* entity is
error-prone, so the two steps are split (as sign is split from magnitude in §8). This
module detects ``Mention``s and binds each to a canonical entity by a defeasible,
confidence-bearing ``REFERS_TO`` edge through the scoped cascade (§3.1):

    local discourse antecedent → an entity already in the graph for that box →
    an entity in the domain-pack taxonomy → unresolved

The default is **conservative**: a binding is ``CONFIRMED`` only when a single referent
clears a high bar; otherwise it stays **open** (one or more ``CANDIDATE`` edges) and the
dependent proposition is marked ``provisional`` and routed to expert triage. An over-eager
binding silently fabricates coreference and corrupts every downstream derivation, so an
open binding is the safer failure (§3.1).

Pure/DB split (the ``core/resolve.py`` discipline): the cascade — detection-schema/prompt,
``normalize``/tokenization, ``block_referents``, ``score_binding``, ``decide_binding``, and
the ``mention``/``refers_to`` write contracts — is DB-free and unit-testable; the LLM does
**detection** only and ``iknos.db.age`` is imported lazily inside the ``ReferenceBinder`` DB
methods so importing this module never pulls in the ``DATABASE_URL`` config singleton.

Binding **scoring is deterministic** (the ``core/resolve.py`` precedent: no LLM in the
scoring path). The LLM, at most, *generates* candidate antecedents during detection; it
never *scores* a binding — **attention weights are not a faithfulness signal** (§3.1). The
score is lexical containment of the mention's surface in a referent's label plus kind/type
agreement — exact attribute evidence, never embedding similarity.

Scope deliberately left to later slices (documented seams):

- **Local-discourse-antecedent stage / pronoun anaphora.** A bare pronoun carries no
  lexical content, so the in-graph-entity stage cannot score it — this slice detects such
  mentions and leaves them **unresolved** (→ proposition ``provisional``), which is the
  correct conservative behaviour, not a silent miss. Binding them needs the discourse-order
  antecedent stage (a dedicated coreference model, §3.1) → a later increment.
- **Taxonomy-anchor stage.** Binding a mention to a domain-pack taxonomy node needs
  entity-linking, now shipped (``core/anchor``, G2.8). Unblocked → a later increment adds the
  ``REFERS_TO``-to-taxonomy cascade stage reusing that linker (G2.8 slice 2 wired the linker
  into ``resolve``/``partwhole``, not yet into this binder).
- **Relational disambiguation.** When several same-kind referents match a definite
  description equally, this slice keeps them all as ``CANDIDATE`` (ambiguous → open); using
  shared-fact/role context to *break* the tie (the ``resolve.score_pair`` relational signal)
  is the natural enhancement → a later increment.
- **Re-binding as belief revision.** A binding computed before a later entity is extracted
  is not recomputed here; re-running only processes not-yet-bound propositions (Action-log
  idempotency). Propagating a re-binding through Layer A/B → Phase 3.
- **Multi-sample / verify confidence** (§3.1 "confidence from consistency + verification"):
  this slice's confidence is the single deterministic binding score; the multi-sample
  detection + verify pass is a later hardening step.
- **Expert-triage queue** for open bindings → Phase 7; this slice marks the proposition
  ``provisional`` and records the ``CANDIDATE`` edges the queue will later consume.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from iknos.core.extract import NodeKind
from iknos.core.llm import LLMClient
from iknos.core.prompts import vocab
from iknos.core.resolve import normalize_label
from iknos.provenance.action_log import record_action
from iknos.types.edges import BindingState

# Note: iknos.db.age is imported lazily inside the ReferenceBinder DB methods (see module
# docstring), so importing this module stays DB-free for the unit tests of the cascade.

# Bump on any change to the binding pipeline (the cascade weights/bars, the blocking or
# scoring logic, the detection prompt/schema). Stored on each bind Action so a REFERS_TO
# edge's producing pipeline is identifiable (mirrors resolve.RESOLVE_SCHEMA_VERSION).
REFER_SCHEMA_VERSION = 1

# Decision bars (§3.1). CONFIRM is deliberately high (conservative — an over-eager binding
# fabricates coreference); the band [CANDIDATE, CONFIRM) records an open, bridgeable binding
# without committing the denotation. TIE_MARGIN keeps near-tied referents all as candidates
# (the §3.1 "multiple candidate targets when ambiguous").
REFER_CONFIRM_BAR = 0.85
REFER_CANDIDATE_BAR = 0.50
REFER_TIE_MARGIN = 0.05

# Scoring weights. Containment (how much of the mention's surface a referent's label covers)
# is the primary lexical signal; an exact normalized-label match and an agreeing kind add to
# it. Chosen so an exact label + agreeing kind reaches the confirm bar, while a definite
# description that is merely *contained in* a fuller named entity ("the bearing" ⊂
# "bearing 3") lands in the candidate band — never an auto-confirm on partial evidence.
_W_CONTAIN = 0.60
_W_EXACT = 0.25
_W_KIND = 0.15


class MentionType(StrEnum):
    """The linguistic category of a surface reference (§3.1) — drives detection, and is

    recorded on the ``Mention`` for audit ("'it' → bearing-3, 0.6"). Orthogonal to the
    referent's :class:`NodeKind`. A ``StrEnum`` so it serializes to a plain string for
    guided decoding / the prompt.

    - ``PRONOUN`` — "it", "they", "this": no lexical content; bindable only via the
      discourse-antecedent stage (a later seam), so detected-but-unresolved in this slice.
    - ``DEFINITE`` — a definite description, "the bearing", "the device".
    - ``PROPER`` — a named reference, "bearing 3", "Acme Corp".
    """

    PRONOUN = "pronoun"
    DEFINITE = "definite"
    PROPER = "proper"


class _MentionOut(BaseModel):
    """One detected mention as emitted by the detector (drives guided decoding).

    Defaults keep a bare ``{"surface": ...}`` response valid (mirrors
    ``extract._EntityOut``). ``kind`` is the detector's guess at whether the referent is an
    acting agent vs a thing — used only to *scope* candidate referents (blocking), never to
    score; an unknown guess simply widens the candidate set, it does not mis-bind.
    """

    surface: str
    mention_type: MentionType = MentionType.DEFINITE
    kind: NodeKind | None = None


class DetectedMentions(BaseModel):
    """Structured-output contract for one proposition's mentions; drives guided decoding."""

    mentions: list[_MentionOut]


MENTION_SCHEMA = DetectedMentions.model_json_schema()


SYSTEM_PROMPT = (
    "You detect the REFERRING EXPRESSIONS in a single statement — the surface phrases that "
    "point at some entity which must be resolved to know what the statement is about. This "
    "is DETECTION ONLY: do not guess which entity each refers to.\n"
    "Detect:\n"
    "- pronouns and demonstratives that stand in for an entity ('it', 'they', 'this unit');\n"
    "- definite descriptions naming a specific entity by category ('the bearing', "
    "'the device');\n"
    "- named references to a specific entity ('bearing 3', 'Acme Corp').\n"
    "Do NOT detect: indefinite/generic noun phrases ('a bearing', 'any pump', 'bearings'), "
    "or bare descriptive words. If the statement contains no referring expression, return an "
    "empty list. Use the statement's own surface form for `surface`.\n"
    "Per-mention fields:\n"
    f"- mention_type ({vocab(MentionType)}): pronoun, definite description, or named "
    "reference.\n"
    f"- kind ({vocab(NodeKind)}) or null: whether the referent is an acting agent (actor) "
    "or a thing acted upon (object), if clear; null if unsure.\n"
    'Example: "It was inspected after bearing 3 failed." -> {"mentions": ['
    '{"surface": "It", "mention_type": "pronoun", "kind": "object"}, '
    '{"surface": "bearing 3", "mention_type": "proper", "kind": "object"}]}.\n'
    'Return JSON of the form {"mentions": [{"surface": "...", "mention_type": "...", '
    '"kind": "..."}]}.'
)


def build_messages(statement: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one proposition's mention detection."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"STATEMENT:\n{statement}"},
    ]


@dataclass(frozen=True)
class Referent:
    """A candidate binding target: a canonical in-graph entity for the box.

    Same-labelled fresh ``Actor``/``Object`` nodes (G2.2 emits one per mention, pre-dedup)
    are **collapsed by normalized label within a kind** into one referent, so a binding does
    not look spuriously ambiguous between two nodes that denote the same thing — and the
    binding targets the group's canonical (lexicographically-min) id, the same representative
    ``resolve.canonical_id`` picks. ``ids`` keeps the full membership for the audit record.
    """

    canonical: uuid.UUID
    ids: frozenset[uuid.UUID]
    label: str
    type: str
    kind: NodeKind

    @property
    def norm(self) -> str:
        return normalize_label(self.label)

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self.norm.split())


def group_referents(
    entities: list[tuple[uuid.UUID, str, str, NodeKind]],
    *,
    exclude_ids: frozenset[uuid.UUID] = frozenset(),
) -> list[Referent]:
    """Collapse fresh entity nodes into canonical referents (per kind + normalized label).

    Input is ``(id, label, type, kind)`` rows. Entities with the same normalized label and
    kind are one referent (the canonical id is their min); a referent with an empty
    normalized label (a punctuation-only / empty surface) is dropped — it can carry no
    lexical binding signal. Deterministic: groups and their canonical ids are a pure function
    of the input set.

    ``exclude_ids`` drops specific entity ids before grouping — the binder passes the
    mention's **own proposition's** entities here so a mention never binds to the fresh node
    extracted from its own clause (that is not coreference, just the clause's entity, already
    captured by ``INVOLVES``). A group emptied by the exclusion is dropped.
    """
    groups: dict[tuple[NodeKind, str], list[tuple[uuid.UUID, str, str]]] = {}
    for eid, label, typ, kind in entities:
        if eid in exclude_ids:
            continue
        norm = normalize_label(label)
        if not norm:
            continue
        groups.setdefault((kind, norm), []).append((eid, label, typ))

    referents: list[Referent] = []
    for (kind, _norm), members in groups.items():
        ids = frozenset(m[0] for m in members)
        canonical = min(ids, key=str)
        # Representative surface form / type: the canonical member's (deterministic tie-break).
        rep = min(members, key=lambda m: str(m[0]))
        referents.append(
            Referent(
                canonical=canonical,
                ids=ids,
                label=rep[1],
                type=rep[2],
                kind=kind,
            )
        )
    return referents


@dataclass(frozen=True)
class Mention:
    """One detected referring expression, assigned a fresh node id (detection ≠ binding)."""

    id: uuid.UUID
    surface: str
    mention_type: MentionType
    kind: NodeKind | None

    @property
    def norm(self) -> str:
        return normalize_label(self.surface)

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self.norm.split())


def block_referents(mention: Mention, referents: list[Referent]) -> list[Referent]:
    """The cheap blocking stage: referents that could bind ``mention`` (§3.1 cascade).

    A referent is a candidate when it shares ≥1 normalized token with the mention and — when
    the detector guessed the mention's ``kind`` — matches that kind. A pronoun (no lexical
    content) shares no token, so it blocks to the empty set: correctly *un-bindable* by the
    lexical in-graph stage (it needs the discourse-antecedent stage, a later seam). The kind
    guess only *narrows*; an absent guess admits both kinds rather than mis-binding.
    """
    m_tokens = mention.tokens
    if not m_tokens:
        return []
    out: list[Referent] = []
    for r in referents:
        if mention.kind is not None and r.kind is not mention.kind:
            continue
        if m_tokens & r.tokens:
            out.append(r)
    return out


def score_binding(mention: Mention, referent: Referent) -> float:
    """Deterministic binding score in [0, 1] from lexical + attribute evidence (§3.1).

    Signals (similarity is **not** one — attention/embeddings never score a binding, §3.1):

    - **Containment.** The fraction of the mention's surface tokens covered by the referent's
      label — a referring expression is typically a *shorter* form of a fuller entity name
      ("the bearing" ⊂ "bearing 3"), so containment (not symmetric overlap) is the right
      lexical signal.
    - **Exact label.** A bonus when the normalized surfaces are identical.
    - **Kind agreement.** A bonus when the detector's kind guess matches the referent (a
      conflicting kind was already excluded in blocking; an absent guess scores neutral).

    Weighted so an exact label + agreeing kind reaches the confirm bar, while mere containment
    in a fuller name lands in the candidate band — partial evidence never auto-confirms.
    """
    m_tokens = mention.tokens
    if not m_tokens:
        return 0.0

    containment = len(m_tokens & referent.tokens) / len(m_tokens)
    exact = 1.0 if mention.norm and mention.norm == referent.norm else 0.0
    kind_agree = 1.0 if mention.kind is not None and mention.kind is referent.kind else 0.0

    score = _W_CONTAIN * containment + _W_EXACT * exact + _W_KIND * kind_agree
    return max(0.0, min(1.0, score))


@dataclass(frozen=True)
class BindingDecision:
    """The cascade's verdict for one mention: the committed/open state + the chosen targets.

    ``state`` is ``CONFIRMED`` (one ``targets`` entry, denotation committed), ``CANDIDATE``
    (one or more open competing targets), or ``None`` (unresolved — no referent cleared the
    candidate bar, ``targets`` empty). ``resolved`` is true only for ``CONFIRMED`` — the
    single signal the binder uses to decide whether the dependent proposition stays
    non-provisional.
    """

    state: BindingState | None
    targets: list[tuple[Referent, float]]

    @property
    def resolved(self) -> bool:
        return self.state is BindingState.CONFIRMED


def decide_binding(mention: Mention, referents: list[Referent]) -> BindingDecision:
    """Score the blocked referents and pick the binding decision (§3.1 conservative default).

    ``CONFIRMED`` only when a **single** referent clears ``REFER_CONFIRM_BAR`` with no
    near-tied rival (within ``REFER_TIE_MARGIN``); a tie at the top, or a best score in the
    candidate band, yields ``CANDIDATE`` edges for the whole top tie-band (§3.1 "multiple
    candidate targets when ambiguous"); nothing above ``REFER_CANDIDATE_BAR`` is unresolved.
    Ties are broken deterministically by canonical id so the chosen targets are reproducible.
    """
    scored = [(r, score_binding(mention, r)) for r in block_referents(mention, referents)]
    viable = [(r, s) for r, s in scored if s >= REFER_CANDIDATE_BAR]
    if not viable:
        return BindingDecision(state=None, targets=[])

    best = max(s for _, s in viable)
    top_band = sorted(
        (rs for rs in viable if best - rs[1] <= REFER_TIE_MARGIN),
        key=lambda rs: (-rs[1], str(rs[0].canonical)),
    )
    if len(top_band) == 1 and best >= REFER_CONFIRM_BAR:
        return BindingDecision(state=BindingState.CONFIRMED, targets=top_band)
    return BindingDecision(state=BindingState.CANDIDATE, targets=top_band)


def mention_to_props(mention: Mention, box: uuid.UUID) -> dict[str, Any]:
    """Flatten a :class:`Mention` to AGE vertex properties — the canonical write contract.

    The single place ``Mention`` serialization lives (cf. ``extract.fact_to_props``). A
    ``Mention`` is a provenance/text object, not a reasoning node, so it carries no
    annotations or bitemporal interval — only its surface, type, and box; its provenance is
    the ``EVIDENCED_BY`` edge(s) to the Span(s) it occurs in (§3.1, §10).
    """
    return {
        "id": str(mention.id),
        "box": str(box),
        "surface": mention.surface,
        "mention_type": str(mention.mention_type),
    }


def refers_to_to_props(
    *, box: uuid.UUID, state: BindingState, strength: float, now: datetime
) -> dict[str, Any]:
    """Flatten a ``REFERS_TO`` edge to AGE properties — the canonical write contract.

    Mirrors ``resolve.same_as_to_props``: ``strength`` is the calibrated binding score (§8),
    the **two §12 annotations** are seeded (``support_count = 1`` — this one binding act
    grounds the edge; ``confidence`` from the strength), and bitemporal fields are stamped
    open. Defeasible and overridable like any scored edge (§3.1/§10.3); a later re-binding is
    belief revision (Phase 3).
    """
    return {
        "box": str(box),
        "state": str(state),
        "strength": strength,
        "support_count": 1,
        "confidence": strength,
        "event_time": None,
        "ingested_at": now.isoformat(),
        "valid_from": now.isoformat(),
        "valid_to": None,
    }


@dataclass(frozen=True)
class BindInput:
    """One unit of work: a proposition (id + text) and the Span(s) it is evidenced by.

    ``span_ids`` are the proposition's ``EVIDENCED_BY`` Spans; each detected ``Mention``
    inherits the same provenance (the mention occurs in those spans, §3.1/§10).
    """

    proposition_id: uuid.UUID
    text: str
    span_ids: list[uuid.UUID]


@dataclass(frozen=True)
class BoundMention:
    """One detected + bound mention, as written/returned by the binder."""

    mention: Mention
    state: BindingState | None
    targets: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class BindResult:
    """The outcome of binding one box: the last Action id and the bound mentions, with the
    propositions marked provisional by an un-confirmed/unresolved binding.

    ``action_id`` is ``None`` only when nothing was pending (every proposition already bound),
    in which case the run is a true no-op and emits no Action.
    """

    action_id: uuid.UUID | None
    bound: list[BoundMention]
    provisional_propositions: list[uuid.UUID]


class ReferenceBinder:
    """The reference-binding operator (§6): detect ``Mention``s → scored ``REFERS_TO``.

    DB-free to construct; the LLM does **detection** only and the binding scoring is pure and
    deterministic. Box-scoped like ``resolve.Resolver`` (the caller binds one source box) and
    three-phase like the extractor (the shared session is unsafe for concurrent use):
    (1) serial idempotency filter against the ``Action`` log, (2) concurrent detection holding
    no DB session, (3) serial per-proposition persist, each its own short transaction.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        sampling: dict[str, object] | None = None,
        concurrency: int = 8,
    ) -> None:
        self.llm = llm
        self.sampling = sampling or {"temperature": 0.0}
        self.concurrency = concurrency

    async def _detect(self, sem: asyncio.Semaphore, statement: str) -> list[Mention]:
        """Detect one statement's referring expressions via guided decoding (LLM, DB-free).

        Each mention gets a **fresh** uuid (detection is not binding). The semaphore bounds
        global LLM concurrency, acquired around the single call so it never nests inside
        another permit (the proposition/extractor permit discipline).
        """
        messages = build_messages(statement)
        async with sem:
            raw = await self.llm.guided_complete(messages, MENTION_SCHEMA, self.sampling)
        out = DetectedMentions.model_validate(raw)
        return [
            Mention(
                id=uuid.uuid4(),
                surface=m.surface,
                mention_type=m.mention_type,
                kind=m.kind,
            )
            for m in out.mentions
        ]

    async def _load_entities(
        self, session: AsyncSession, box: uuid.UUID
    ) -> tuple[list[tuple[uuid.UUID, str, str, NodeKind]], dict[uuid.UUID, set[uuid.UUID]]]:
        """Load the box's in-graph entities with the proposition each was extracted from.

        The candidate binding-target pool (the in-graph-entity cascade stage, §3.1) is the
        box's ``Actor``/``Object`` nodes the extractor wrote; the join to the owning
        Proposition (each entity ``INVOLVES`` exactly one box ``Fact``, which is
        ``EVIDENCED_BY`` its Proposition) lets the binder exclude a mention's **own**
        proposition's entities so it never self-binds (see :func:`group_referents`). Returns
        the ``(id, label, type, kind)`` rows (deduped) and the ``proposition → own entity ids``
        map.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        bx = str(box)
        rows_acc: dict[uuid.UUID, tuple[uuid.UUID, str, str, NodeKind]] = {}
        by_prop: dict[uuid.UUID, set[uuid.UUID]] = {}
        for kind, label in ((NodeKind.ACTOR, "Actor"), (NodeKind.OBJECT, "Object")):
            rows = await execute_cypher(
                session,
                f"MATCH (f:Fact {{box: '{bx}'}})-[:INVOLVES]->(e:{label} {{box: '{bx}'}}) "
                "MATCH (f)-[:EVIDENCED_BY]->(p:Proposition) "
                "RETURN e.id, e.label, e.type, p.id",
                returns="eid agtype, label agtype, typ agtype, pid agtype",
            )
            for eid, lab, typ, pid in rows:
                ent_id = uuid.UUID(unquote_agtype(eid))
                rows_acc[ent_id] = (ent_id, unquote_agtype(lab), unquote_agtype(typ), kind)
                by_prop.setdefault(uuid.UUID(unquote_agtype(pid)), set()).add(ent_id)
        return list(rows_acc.values()), by_prop

    async def _load_propositions(self, session: AsyncSession, box: uuid.UUID) -> list[BindInput]:
        """Load the box's propositions (those a box ``Fact`` is ``EVIDENCED_BY``) + their spans.

        Propositions carry no ``box`` property (the Phase-1 deviation, ``nodes.Proposition``),
        so the box scope is reached through the Facts the extractor wrote: a box ``Fact`` is
        ``EVIDENCED_BY`` its Proposition, which is in turn ``EVIDENCED_BY`` its Span(s). One
        query collects the proposition id + text and aggregates its span ids in Python.
        """
        from iknos.db.age import execute_cypher, unquote_agtype

        bx = str(box)
        rows = await execute_cypher(
            session,
            f"MATCH (f:Fact {{box: '{bx}'}})-[:EVIDENCED_BY]->(p:Proposition) "
            "OPTIONAL MATCH (p)-[:EVIDENCED_BY]->(s:Span) "
            "RETURN p.id, p.text, s.id",
            returns="pid agtype, ptext agtype, sid agtype",
        )
        agg: dict[uuid.UUID, dict[str, Any]] = {}
        for pid, ptext, sid in rows:
            key = uuid.UUID(unquote_agtype(pid))
            rec = agg.setdefault(key, {"text": unquote_agtype(ptext), "spans": []})
            if sid is not None and str(sid) != "null":
                rec["spans"].append(uuid.UUID(unquote_agtype(sid)))
        return [
            BindInput(proposition_id=key, text=rec["text"], span_ids=rec["spans"])
            for key, rec in agg.items()
        ]

    async def _already_bound(self, session: AsyncSession, proposition_id: uuid.UUID) -> bool:
        """Whether this proposition's mentions were already detected + bound (idempotency).

        Action-table backed (the single source of truth), mirroring
        ``extract._already_extracted``. Re-binding under a changed pipeline, or after new
        entities arrive (belief revision), is a later concern; this slice only skips an
        already-bound proposition so a re-run over a box is a no-op on settled propositions.
        """
        row = await session.execute(
            text(
                "SELECT 1 FROM actions WHERE actor = 'reference-binder' AND action_type = 'bind' "
                "AND inputs->>'proposition' = :pid LIMIT 1"
            ),
            {"pid": str(proposition_id)},
        )
        return row.scalar_one_or_none() is not None

    async def _persist(
        self,
        session: AsyncSession,
        item: BindInput,
        box: uuid.UUID,
        mentions: list[Mention],
        referents: list[Referent],
    ) -> tuple[uuid.UUID, list[BoundMention], bool]:
        """Persist one proposition's Mentions + REFERS_TO + provisional flag + Action.

        Returns ``(action_id, bound_mentions, proposition_made_provisional)``. One short
        transaction per proposition (the extractor's ``_persist`` discipline). A proposition
        is marked ``provisional`` (OR-folded, never cleared — the proposition-layer discipline)
        when any of its mentions is unresolved or only candidate-bound (§3.1).
        """
        from iknos.db.age import execute_cypher, merge_edge, merge_vertex

        now = datetime.now(UTC)
        bound: list[BoundMention] = []
        any_open = False

        for mention in mentions:
            await merge_vertex(session, "Mention", mention_to_props(mention, box))
            # Provenance (§10.2): the Mention occurs in the proposition's source Span(s).
            for sid in item.span_ids:
                await merge_edge(
                    session,
                    src_id=mention.id,
                    dst_id=sid,
                    label="EVIDENCED_BY",
                    props={"box": str(box)},
                )

            decision = decide_binding(mention, referents)
            targets: list[uuid.UUID] = []
            # targets is non-empty iff state is CONFIRMED/CANDIDATE (never None — unresolved
            # carries no targets), so the binding state is committed here.
            for referent, strength in decision.targets:
                assert decision.state is not None
                await merge_edge(
                    session,
                    src_id=mention.id,
                    dst_id=referent.canonical,
                    label="REFERS_TO",
                    props=refers_to_to_props(
                        box=box, state=decision.state, strength=strength, now=now
                    ),
                )
                targets.append(referent.canonical)
            bound.append(BoundMention(mention=mention, state=decision.state, targets=targets))
            if not decision.resolved:
                any_open = True

        if any_open:
            # OR-fold the system gate (§3.1): a proposition resting on an unresolved or merely
            # candidate-bound mention is provisional. Never cleared here (the G1.6 discipline).
            await execute_cypher(
                session,
                f"MATCH (p:Proposition {{id: '{item.proposition_id}'}}) SET p.provisional = true",
            )

        action_id = await record_action(
            session,
            actor="reference-binder",
            action_type="bind",
            inputs={
                "proposition": str(item.proposition_id),
                "spans": [str(s) for s in item.span_ids],
                "box": str(box),
                "referents": [str(r.canonical) for r in referents],
                "schema_version": REFER_SCHEMA_VERSION,
            },
            outputs={
                "mentions": [str(m.mention.id) for m in bound],
                "confirmed": [
                    f"{m.mention.id}->{t}"
                    for m in bound
                    if m.state is BindingState.CONFIRMED
                    for t in m.targets
                ],
                "candidate": [
                    f"{m.mention.id}->{t}"
                    for m in bound
                    if m.state is BindingState.CANDIDATE
                    for t in m.targets
                ],
                "unresolved": [str(m.mention.id) for m in bound if m.state is None],
                "provisional": any_open,
            },
            model=self.llm.model,
            sampling=self.sampling,
        )
        await session.commit()
        return action_id, bound, any_open

    async def bind_box(self, session: AsyncSession, box: uuid.UUID) -> BindResult:
        """Bind one box: detect mentions in its propositions and resolve them to entities.

        The §6 operator shape, box-scoped. Loads the box's entity pool once, then for each
        not-yet-bound proposition detects its mentions (concurrent, DB-free) and binds them
        against the referents **other than that proposition's own** entities (no self-binding),
        marking provisional where the binding stays open. Emits one ``bind`` Action per
        proposition (``actor="reference-binder"``); idempotent on settled propositions. Call
        **after** extraction has populated the box (and, ideally, after resolution — binding to
        canonical components is cleaner — though grouping referents by label makes this slice
        robust to running before resolve).
        """
        entities, by_prop = await self._load_entities(session, box)
        items = await self._load_propositions(session, box)

        # Phase 1: idempotency filter (serial reads on the shared session).
        pending: list[BindInput] = []
        for item in items:
            if not await self._already_bound(session, item.proposition_id):
                pending.append(item)

        # Phase 2: concurrent detection, DB-free, bounded by a single shared semaphore.
        sem = asyncio.Semaphore(self.concurrency)
        detected = await asyncio.gather(*(self._detect(sem, item.text) for item in pending))

        # Phase 3: serial persistence — one short transaction per proposition. Each
        # proposition's referent pool excludes its own extracted entities (no self-binding).
        last_action: uuid.UUID | None = None
        all_bound: list[BoundMention] = []
        provisional: list[uuid.UUID] = []
        for item, mentions in zip(pending, detected, strict=True):
            referents = group_referents(
                entities, exclude_ids=frozenset(by_prop.get(item.proposition_id, set()))
            )
            action_id, bound, made_provisional = await self._persist(
                session, item, box, mentions, referents
            )
            last_action = action_id
            all_bound.extend(bound)
            if made_provisional:
                provisional.append(item.proposition_id)

        return BindResult(
            action_id=last_action,
            bound=all_bound,
            provisional_propositions=provisional,
        )
