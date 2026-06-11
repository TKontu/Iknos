"""Self-consistency over multi-sample extraction (Phase 1, G1.3) — the consistency half
of §3.1's "confidence comes from consistency *and* verification".

The extractor is sampled N times for one span (G1.3); a proposition the model reliably
re-produces across samples is *stable* (high agreement), one it emits only occasionally is
*unstable* — the perception-layer analogue of self-consistency decoding. This module turns N
variable-length proposition lists into clusters of semantically-equivalent propositions, an
``agreement`` ∈ [0, 1] per cluster, and a single canonical representative per cluster. The
agreement signal is then combined into ``faithfulness`` (multiplicatively) by
:func:`~iknos.types.epistemic.combine_faithfulness`.

Deliberately **pure**: no DB, no torch, no LLM. It operates on already-computed passage
embeddings (``EmbeddingSubstrate.embed_passages`` — L2-normalized bge-m3 vectors, so cosine is
a plain dot product) and is unit-testable with hand-built toy vectors, exactly like the scoring
algebra in ``types/epistemic.py``.
"""

from dataclasses import dataclass

from iknos.types.epistemic import Attribution, EpistemicClass, Modality, Polarity

# Cosine threshold above which two extracted propositions are treated as "the same claim"
# for the agreement count. Tunable — Trial A5 fits it against a labeled corpus (single-pass
# vs multi-sample). bge-m3 vectors are normalized, so this is a dot-product threshold.
DEFAULT_AGREEMENT_THRESHOLD: float = 0.86


@dataclass(frozen=True)
class Candidate:
    """One proposition emitted by one extraction sample (internal to the inference phase).

    Carries the extractor's epistemic fields alongside the text + embedding so the canonical
    (medoid) proposition keeps its operators *coherent* — text and operators always come from
    the same sample, never spliced across samples. ``sample_index`` identifies which of the N
    samples produced it (drives the distinct-sample agreement count); ``position`` is its index
    within that sample's list (only a deterministic tie-break).
    """

    text: str
    polarity: Polarity
    modality: Modality
    attribution: Attribution
    scope: str
    epistemic_class: EpistemicClass
    embedding: list[float]
    sample_index: int
    position: int


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Embeddings are L2-normalized so this is a dot product, but the
    norms are divided out defensively (a zero vector → 0.0, never a divide-by-zero)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(dot / (na * nb))


def _ordered(candidates: list[Candidate]) -> list[Candidate]:
    """Candidates in the canonical (sample_index, position) order — the single ordering that
    makes clustering and medoid tie-breaks deterministic regardless of caller input order."""
    return sorted(candidates, key=lambda c: (c.sample_index, c.position))


def cluster_candidates(
    candidates: list[Candidate], *, threshold: float = DEFAULT_AGREEMENT_THRESHOLD
) -> list[list[Candidate]]:
    """Group candidates into clusters of semantically-equivalent propositions (G1.3).

    Deterministic greedy assignment against each cluster's *representative* (its opening
    member): in fixed ``(sample_index, position)`` order, each candidate joins the first
    existing cluster whose representative similarity ≥ ``threshold``, else opens a new cluster.
    Greedy-against-representative (not chaining connected-components) so a candidate cannot
    transitively merge two clusters it only weakly resembles. Order is fixed, so the result is
    reproducible — important for replay (§10).

    Degenerate cases fall out naturally: N=1 (or all-unique extractions) → every candidate is
    its own singleton cluster → agreement 1/1 each in single-sample mode.
    """
    clusters: list[list[Candidate]] = []
    representatives: list[list[float]] = []  # parallel to clusters: each cluster's opener
    for cand in _ordered(candidates):
        for i, rep in enumerate(representatives):
            if _cosine(cand.embedding, rep) >= threshold:
                clusters[i].append(cand)
                break
        else:
            clusters.append([cand])
            representatives.append(cand.embedding)
    return clusters


def cluster_candidates_partitioned(
    candidates: list[Candidate], *, threshold: float = DEFAULT_AGREEMENT_THRESHOLD
) -> list[list[Candidate]]:
    """Cluster, but only *within* identical ``(polarity, epistemic_class)`` partitions (G1.14).

    Sentence-embedding cosine cannot tell a claim from its negation — a claim and its negation
    typically sit at cosine > 0.9 — so plain :func:`cluster_candidates` co-clusters asserted and
    negated variants of the same claim, reporting maximal agreement on precisely the polarity
    instability §3.1 exists to catch. Polarity and epistemic class are therefore treated as
    **identity** (hard partition); modality stays soft (it varies legitimately across phrasings and
    is left to the cosine clustering). The inner algorithm is the untouched greedy-against-
    representative :func:`cluster_candidates`; this only restricts what may co-cluster.

    Partitions are visited in sorted ``(polarity, epistemic_class)`` order and candidates within a
    partition keep their :func:`_ordered` order, so the result is deterministic (replay, §10).
    """
    groups: dict[tuple[Polarity, EpistemicClass], list[Candidate]] = {}
    for cand in _ordered(candidates):
        groups.setdefault((cand.polarity, cand.epistemic_class), []).append(cand)
    clusters: list[list[Candidate]] = []
    for key in sorted(groups):
        clusters.extend(cluster_candidates(groups[key], threshold=threshold))
    return clusters


@dataclass(frozen=True)
class Consolidated:
    """One consolidated proposition out of multi-sample clustering (G1.3 + G1.14).

    ``canonical`` is the cluster's medoid (text + operators coherent); ``agreement`` is the
    distinct-sample fraction; ``polarity_unstable`` is set when this cluster is one half of a
    **polarity twin** — a same-claim cluster of the opposite polarity also survived, i.e. the
    sampler wavered on the sign of the claim. A polarity-unstable proposition is quarantined
    (``provisional``) regardless of how the verifier scores it.
    """

    canonical: Candidate
    agreement: float
    polarity_unstable: bool


def consolidate_samples(
    candidates: list[Candidate], *, n_samples: int, threshold: float = DEFAULT_AGREEMENT_THRESHOLD
) -> tuple[list[Consolidated], list[tuple[int, int]]]:
    """Turn N samples' candidates into consolidated propositions + the polarity-twin pairs (G1.14).

    Polarity-aware clustering (so a 3-assert / 2-negate split yields a 0.6 cluster and a 0.4
    cluster, never one 1.0 cluster), then **twin detection**: any two clusters of opposite polarity
    whose medoids' cosine ≥ ``threshold`` are the same affirmative claim asserted in one partition
    and negated in another. Both halves are flagged ``polarity_unstable`` (→ ``provisional``); each
    keeps its own distinct-sample agreement (the instability is recorded as a negative signal, not
    by collapsing the two). Returns the consolidated list (cluster order) and the twin pairs as
    index pairs into that list (for the extract ``Action`` / Trial A5).
    """
    clusters = cluster_candidates_partitioned(candidates, threshold=threshold)
    canon = [canonical_of(cluster) for cluster in clusters]
    unstable = [False] * len(clusters)
    twins: list[tuple[int, int]] = []
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if canon[i].polarity != canon[j].polarity and (
                _cosine(canon[i].embedding, canon[j].embedding) >= threshold
            ):
                unstable[i] = unstable[j] = True
                twins.append((i, j))
    consolidated = [
        Consolidated(
            canonical=canon[k],
            agreement=agreement_of(clusters[k], n_samples=n_samples),
            polarity_unstable=unstable[k],
        )
        for k in range(len(clusters))
    ]
    return consolidated, twins


def agreement_of(cluster: list[Candidate], *, n_samples: int) -> float:
    """Agreement ∈ [0, 1] for a cluster = fraction of the N samples that produced this claim.

    Counts **distinct** ``sample_index`` values, not raw members — one sample emitting two
    near-duplicate propositions must not inflate agreement. Clamped to 1.0 defensively.
    """
    if n_samples < 1:
        raise ValueError(f"n_samples must be >= 1, got {n_samples!r}")
    distinct_samples = len({c.sample_index for c in cluster})
    return min(distinct_samples / n_samples, 1.0)


def canonical_of(cluster: list[Candidate]) -> Candidate:
    """The cluster's canonical proposition = its **medoid** (member with the highest mean
    cosine to the others), tie-broken by smallest ``(sample_index, position)``.

    The medoid is the most central phrasing, so the persisted proposition is not an outlier
    wording that happened to open the cluster. A singleton returns its only member.
    """
    if not cluster:
        raise ValueError("cannot pick a canonical from an empty cluster")
    members = _ordered(cluster)  # iterate in tie-break order; strict > keeps the first winner
    if len(members) == 1:
        return members[0]
    best: Candidate | None = None
    best_score = float("-inf")
    for c in members:
        others = [o for o in members if o is not c]
        score = sum(_cosine(c.embedding, o.embedding) for o in others) / len(others)
        if score > best_score:
            best_score = score
            best = c
    assert best is not None  # members is non-empty
    return best
