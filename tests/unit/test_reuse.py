"""Unit tests for the G1.7b cross-doc reuse reconstruction — pure, no DB/LLM/torch.

The DB lookup (``find_reusable_extraction``) is exercised live in
``tests/integration/test_extraction_reuse.py``; here we pin the pure half: an AGE ``properties(p)``
map round-trips back into a :class:`CachedProposition` with its enums rebuilt, its
faithfulness/agreement (which may be null) preserved exactly, and its ``provisional_reasons`` (R8 —
a JSON-string list property) decoded back to a ``list[str]``. This is the seam where a drift between
how ``_persist`` *writes* a proposition vertex and how reuse *reads* it back would corrupt a replay.
"""

from iknos.core.reuse import CachedProposition, _cached_proposition_from_props
from iknos.types.epistemic import (
    Attribution,
    EpistemicClass,
    Modality,
    Polarity,
    Routing,
)


def test_reconstructs_full_proposition_from_props() -> None:
    """A fully-populated vertex (verified, multi-sample) rebuilds with every field typed."""
    props = {
        "id": "11111111-1111-1111-1111-111111111111",
        "text": "The bearing failed.",
        "polarity": "negated",
        "modality": "probable",
        "attribution": "named-source",
        "scope": "during startup",
        "epistemic_class": "judgement",
        "routing": "judgement",
        "faithfulness": 0.4,
        # provisional_reasons persists as a JSON-string list (cypher_map json-encodes lists);
        # the legacy boolean is still present but deliberately not read (R8).
        "provisional_reasons": '["low_faithfulness", "unresolved_reference"]',
        "provisional": True,
        "agreement": 1 / 3,
    }
    cached = _cached_proposition_from_props(props)
    assert cached == CachedProposition(
        text="The bearing failed.",
        polarity=Polarity.NEGATED,
        modality=Modality.PROBABLE,
        attribution=Attribution.NAMED_SOURCE,
        scope="during startup",
        epistemic_class=EpistemicClass.JUDGEMENT,
        routing=Routing.JUDGEMENT,
        faithfulness=0.4,
        provisional_reasons=["low_faithfulness", "unresolved_reference"],
        agreement=1 / 3,
    )
    # Enums are real enum members, not bare strings — a replay persists them identically.
    assert isinstance(cached.polarity, Polarity)
    assert isinstance(cached.routing, Routing)


def test_null_faithfulness_and_agreement_preserved() -> None:
    """The degraded/single-sample state (no verifier, n=1) reads back as None, not 0.0 — so a
    replay reproduces 'unknown faithfulness', never a fabricated score."""
    props = {
        "id": "22222222-2222-2222-2222-222222222222",
        "text": "The surface shows indentations.",
        "polarity": "asserted",
        "modality": "categorical",
        "attribution": "document",
        "scope": "",
        "epistemic_class": "observation",
        "routing": "fact",
        "faithfulness": None,
        # An un-provisional / pre-R8 vertex: the reasons property is absent → empty set.
        "agreement": None,
    }
    cached = _cached_proposition_from_props(props)
    assert cached.faithfulness is None
    assert cached.provisional_reasons == []
    assert cached.agreement is None
    assert cached.routing is Routing.FACT
