"""Unit tests for the G1.7b cross-doc reuse reconstruction — pure, no DB/LLM/torch.

The DB lookup (``find_reusable_extraction``) is exercised live in
``tests/integration/test_extraction_reuse.py``; here we pin the pure half: an AGE ``properties(p)``
map round-trips back into a :class:`CachedProposition` with its enums rebuilt, its
faithfulness/agreement (which may be null) preserved exactly, and its R8 ``provisional_reasons``
decoded (incl. the pre-R8 legacy-boolean fallback). This is the seam where a drift between how
``_write_propositions`` *writes* a proposition vertex and how reuse *reads* it back would corrupt a
replay.
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
        # cypher_map JSON-encodes the reason list, so it reads back as a JSON string (R8).
        "provisional_reasons": '["low_faithfulness"]',
        "provisional": True,  # legacy boolean still written for the transition window
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
        provisional_reasons=["low_faithfulness"],
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
        # no provisional_reasons property, legacy boolean null → not provisional
        "provisional": None,
        "agreement": None,
    }
    cached = _cached_proposition_from_props(props)
    assert cached.faithfulness is None
    assert cached.provisional_reasons == []
    assert cached.agreement is None
    assert cached.routing is Routing.FACT


def test_pre_r8_legacy_boolean_reconstructs_a_reason() -> None:
    """A node written before R8 has no provisional_reasons, only the legacy boolean. A True must
    not silently clear on replay: recover the faithfulness-derived reason, or the polarity reason
    when faithfulness doesn't explain it (a verifier-off twin)."""
    base = {
        "id": "33333333-3333-3333-3333-333333333333",
        "text": "x",
        "polarity": "asserted",
        "modality": "categorical",
        "attribution": "document",
        "scope": "",
        "epistemic_class": "observation",
        "routing": "fact",
        "agreement": None,
    }
    low = _cached_proposition_from_props({**base, "faithfulness": 0.4, "provisional": True})
    assert low.provisional_reasons == ["low_faithfulness"]
    twin = _cached_proposition_from_props({**base, "faithfulness": None, "provisional": True})
    assert twin.provisional_reasons == ["polarity_unstable"]
    clean = _cached_proposition_from_props({**base, "faithfulness": 0.9, "provisional": False})
    assert clean.provisional_reasons == []
