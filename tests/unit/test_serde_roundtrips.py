"""Property-based round-trip tests for the manual AGE serde pairs (W8).

Each reasoning-graph value type that persists to AGE has a hand-written ``flatten`` / ``from_props``
pair (no ORM mapper — the graph lives in AGE, not SQLAlchemy). The §10.2 review flagged these as a
silent-corruption class: a field added to one half and not the other, or a list field that
``cypher_map`` JSON-encodes on write but ``from_props`` forgets to decode on read, round-trips wrong
with **no** error. The existing tests pin specific cases; these pin the **invariant** —
``from_props(flatten(x)) == x`` for *arbitrary* ``x`` — across the whole input space via Hypothesis.

**The AGE storage form is simulated, not mocked.** ``cypher_map`` JSON-encodes any non-scalar
(list/dict) value into a *string* property and leaves scalars (str/bool/int/float/None) as-is; the
read returns list properties as those JSON strings, which ``from_props`` must decode.
:func:`_age_stored` reproduces exactly that transform, so the round-trip is tested through the
**read shape AGE returns**, not only the in-memory dict — where a forgotten ``json.loads`` corrupts.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from iknos.boxes.serde import box_from_props, box_to_props
from iknos.core.resolve import same_as_to_props
from iknos.types.edges import SameAsState
from iknos.types.governance import (
    Sensitivity,
    SensitivityLevel,
    SourceInterest,
)
from iknos.types.nodes import Box, BoxStatus, Tier


def _age_stored(props: dict[str, Any]) -> dict[str, Any]:
    """The property map as AGE returns it after a ``cypher_map`` write — the faithful read shape.

    ``cypher_map`` JSON-encodes a list/dict value into a string property (``json.dumps``) and passes
    scalars through unchanged; the read returns those list properties as the JSON strings, which the
    ``from_props`` half must decode. Reproducing the transform here tests the round-trip against the
    shape AGE actually hands back — the path a forgotten decode silently corrupts.
    """
    return {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in props.items()}


# Surrogate-free text: ``json.dumps`` cannot encode lone surrogates, and neither can a real AGE
# property, so they are out of the domain for a list field that serializes through JSON.
_TEXT = st.text(st.characters(blacklist_categories=("Cs",)), max_size=24)
_STR_SET = st.frozensets(_TEXT, max_size=5)


# --- Sensitivity.flatten / from_props (§9.1) ----------------------------------------------------

_sensitivities = st.builds(
    Sensitivity, level=st.sampled_from(list(SensitivityLevel)), compartments=_STR_SET
)


@given(s=_sensitivities)
def test_sensitivity_round_trips_in_memory(s: Sensitivity) -> None:
    assert Sensitivity.from_props(s.flatten()) == s


@given(s=_sensitivities)
def test_sensitivity_round_trips_through_age_storage(s: Sensitivity) -> None:
    # The compartment list comes back JSON-encoded from AGE; from_props must decode it.
    assert Sensitivity.from_props(_age_stored(s.flatten())) == s


# --- Box serde (incl. the SourceInterest pair it embeds) ----------------------------------------

_source_interests = st.one_of(
    st.none(),
    st.builds(SourceInterest, role=st.none() | _TEXT, stake=_STR_SET),
)

_boxes = st.builds(
    Box,
    id=st.uuids(),
    name=_TEXT,
    tier=st.sampled_from(list(Tier)),
    version=_TEXT,
    source=_TEXT,
    reliability_prior=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    interest=_source_interests,
    valid_from=st.datetimes(timezones=st.just(UTC)),
    valid_to=st.none() | st.datetimes(timezones=st.just(UTC)),
    status=st.sampled_from(list(BoxStatus)),
)


@given(box=_boxes)
def test_box_round_trips_in_memory(box: Box) -> None:
    assert box_from_props(box_to_props(box)) == box


@given(box=_boxes)
def test_box_round_trips_through_age_storage(box: Box) -> None:
    # interest_stake is the JSON-encoded list field; the None-vs-known-empty interest distinction
    # must also survive (an absent interest omits both keys, so it reads back as None).
    assert box_from_props(_age_stored(box_to_props(box))) == box


@given(role=st.none() | _TEXT, stake=_STR_SET)
def test_source_interest_known_vs_unknown_is_preserved(
    role: str | None, stake: frozenset[str]
) -> None:
    # A *known* interest (even empty) round-trips as itself; an absent one reads back as None — the
    # distinction SourceInterest exists to preserve, exercised through its only inverse (box serde).
    known = Box(
        id=uuid.uuid4(),
        name="b",
        tier=Tier.CASE,
        version="1",
        source="s",
        reliability_prior=0.5,
        interest=SourceInterest(role=role, stake=stake),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert box_from_props(_age_stored(box_to_props(known))).interest == SourceInterest(
        role=role, stake=stake
    )


# --- same_as_to_props (write-only: the round-trip is serialization stability) --------------------


@given(
    box=st.uuids(),
    state=st.sampled_from(list(SameAsState)),
    strength=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    now=st.datetimes(timezones=st.just(UTC)),
)
def test_same_as_props_are_all_age_scalars_and_survive_storage(
    box: Any, state: SameAsState, strength: float, now: datetime
) -> None:
    props = same_as_to_props(box=box, state=state, strength=strength, now=now)
    # Every value is a cypher_map scalar (no nested list/dict that would be lossily JSON-blobbed),
    # so the stored form is byte-identical to the produced form — the serde cannot silently corrupt.
    assert all(v is None or isinstance(v, (str, int, float, bool)) for v in props.values())
    assert _age_stored(props) == props
    # The seeded §12 annotations + bitemporal-open contract the writer promises.
    assert props["support_count"] == 1
    assert props["confidence"] == strength
    assert props["state"] == str(state)
    assert props["valid_to"] is None and props["event_time"] is None
    assert props["ingested_at"] == now.isoformat() == props["valid_from"]
