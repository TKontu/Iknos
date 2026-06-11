"""Property-based fuzzing of the cypher_map escaping boundary (G1.17 R7).

``db/age.py::cypher_map`` hand-rolls the escaping that inlines values into an AGE Cypher map
literal (AGE cannot bind parameters into the Cypher body). Document text and LLM output — both
adversarial-by-nature — cross this boundary, so a hole here is an injection bug. These pure tests
fuzz the *escaping logic* itself: a string value round-trips losslessly through the single-quoted
Cypher literal cypher_map emits, and no value can ever break out of that literal. The companion
round-trip through a *live* AGE engine (the escaping must also match AGE's own grammar) lives in
``tests/integration/test_age_cypher_map.py``.
"""

from hypothesis import given
from hypothesis import strategies as st

from iknos.db.age import _dollar_quote_tag, cypher_map

_PREFIX = "{k: '"
_SUFFIX = "'}"


def _extract_literal_body(serialized: str) -> str:
    """The escaped body of ``cypher_map({"k": v})`` → the text between the single quotes."""
    assert serialized.startswith(_PREFIX) and serialized.endswith(_SUFFIX)
    return serialized[len(_PREFIX) : -len(_SUFFIX)]


def _decode_cypher_string(esc: str) -> str:
    """Inverse of cypher_map's string escaping: ``\\\\`` → ``\\`` and ``\\'`` → ``'``."""
    out: list[str] = []
    i = 0
    while i < len(esc):
        c = esc[i]
        if c == "\\" and i + 1 < len(esc) and esc[i + 1] in ("\\", "'"):
            out.append(esc[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _has_no_unescaped_quote(esc: str) -> bool:
    """True if no single quote in ``esc`` can terminate the literal early (each is backslashed).

    Scans escape pairs: a ``\\`` consumes the next char as escaped; any bare ``'`` reached outside
    such a pair would close the Cypher string and inject — the exact failure this guards.
    """
    i = 0
    while i < len(esc):
        if esc[i] == "\\" and i + 1 < len(esc):
            i += 2  # an escaped pair — the next char is consumed, cannot break out
            continue
        if esc[i] == "'":
            return False
        i += 1
    return True


@given(st.text())
def test_cypher_map_string_round_trips(value: str) -> None:
    # The escaped literal decodes back to exactly the original value — escaping is lossless for
    # any text, including quotes, backslashes, unicode, newlines, and agtype-syntax fragments.
    body = _extract_literal_body(cypher_map({"k": value}))
    assert _decode_cypher_string(body) == value


@given(st.text())
def test_cypher_map_value_cannot_break_out_of_literal(value: str) -> None:
    # No value can terminate the single-quoted literal early (injection safety).
    body = _extract_literal_body(cypher_map({"k": value}))
    assert _has_no_unescaped_quote(body)


@given(
    st.dictionaries(
        st.sampled_from(["a", "b", "c", "d"]),
        st.one_of(
            st.text(),
            st.booleans(),
            st.integers(),
            st.none(),
            st.floats(allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
    )
)
def test_cypher_map_is_a_balanced_map_literal(props: dict) -> None:
    # Whatever the value types, the output is a single brace-delimited map with one entry per key.
    serialized = cypher_map(props)
    assert serialized.startswith("{") and serialized.endswith("}")
    # One ``key: value`` part per key (keys here are quote/backslash-free, so counting commas at
    # the top level is exact: the only commas are entry separators).
    assert serialized.count(": ") == len(props)


@given(st.text(alphabet="\\'\"{}[]:,\x00\n\t ", min_size=0, max_size=12))
def test_cypher_map_adversarial_alphabet_round_trips(value: str) -> None:
    # Concentrate on the metacharacters that matter at this boundary: quotes, backslashes,
    # agtype/JSON punctuation, NUL, and whitespace.
    body = _extract_literal_body(cypher_map({"k": value}))
    assert _decode_cypher_string(body) == value
    assert _has_no_unescaped_quote(body)


# --- collision-proof dollar-quote tag (G1.17 R7 — fuzzing caught the $$ break-out) ---


def test_dollar_quote_tag_default_when_absent() -> None:
    assert _dollar_quote_tag("MATCH (n) RETURN n") == "$iknos$"


def test_dollar_quote_tag_avoids_collision() -> None:
    # A body literally containing the default tag forces the next one, and so on.
    assert _dollar_quote_tag("a $iknos$ b") == "$iknos1$"
    assert _dollar_quote_tag("a $iknos$ b $iknos1$ c") == "$iknos2$"


def test_dollar_quote_tag_unaffected_by_double_dollar() -> None:
    # A value carrying $$ (the original break-out) does not collide with the $iknos$ tag.
    assert _dollar_quote_tag("CREATE (n {v: '$$ break out $$'})") == "$iknos$"


@given(st.text())
def test_dollar_quote_tag_never_occurs_in_body(body: str) -> None:
    # The whole point: the chosen tag can never appear in the body, so no body content can
    # terminate the dollar-quoted SQL string early.
    assert _dollar_quote_tag(body) not in body
