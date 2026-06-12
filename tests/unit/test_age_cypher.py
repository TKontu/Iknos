"""Unit tests for the SQL/AGE statement assembly — the two-layer injection boundary (M1, V11).

``db/age.py`` is the Cypher builder every graph write flows through; the 2026-06-11 review
(M1) flagged its injection defense as covered only *transitively* by integration writes.
The Cypher-literal layer (``cypher_map``) and the dollar-tag selection (``_dollar_quote_tag``)
are already pinned in ``test_cypher_map.py``. This module pins the remaining piece: the SQL
**assembly** in ``_build_cypher_sql`` — the pure, config-free seam of :func:`cypher` — where
the body is wrapped in a dollar-quoted SQL string so its single-quoted Cypher literals need
no SQL escaping. The risk it guards: a body that closes the quote early and injects raw SQL.
"""

import pytest

from iknos.db.age import _build_cypher_sql, _dollar_quote_tag

_RETURNS = "result agtype"


def test_wraps_body_in_a_select_cypher_invocation() -> None:
    sql = _build_cypher_sql("iknos", "MATCH (n) RETURN n", _RETURNS)
    assert sql.startswith("SELECT * FROM cypher('iknos', ")
    assert sql.endswith(") AS (result agtype)")


def test_graph_name_and_returns_are_interpolated() -> None:
    sql = _build_cypher_sql("other_graph", "MATCH (n) RETURN n", "x agtype, y agtype")
    assert "cypher('other_graph', " in sql
    assert sql.endswith(" AS (x agtype, y agtype)")


def test_default_tag_wraps_the_body_exactly_twice() -> None:
    body = "MATCH (n) RETURN n"
    sql = _build_cypher_sql("iknos", body, _RETURNS)
    # A plain body has no $iknos$, so the default tag is used, opening and closing once each.
    assert sql.count("$iknos$") == 2
    assert f"$iknos$ {body} $iknos$" in sql


def test_no_bare_double_dollar_delimiter_is_ever_used() -> None:
    # The whole point of the per-body tag: the SQL string is never wrapped in a fixed `$$`,
    # which a body value carrying `$$` (LaTeX, raw doc text) could otherwise terminate early.
    body = "CREATE (n {v: '$$ break out; DROP TABLE actions; --'})"
    sql = _build_cypher_sql("iknos", body, _RETURNS)
    tag = _dollar_quote_tag(body)
    assert tag == "$iknos$"  # the body's $$ does not collide with the iknos tag
    assert tag not in body  # so the body cannot close the quote
    assert sql.count(tag) == 2
    assert f"{tag} {body} {tag}" in sql


def test_body_containing_the_tag_forces_escalation_and_stays_balanced() -> None:
    # A body that itself contains $iknos$ (and even $iknos1$) must not be able to terminate
    # the wrapper: the tag escalates past every occurrence, still appearing exactly twice.
    body = "CREATE (n {note: 'contains $iknos$ and $iknos1$ literally'})"
    sql = _build_cypher_sql("iknos", body, _RETURNS)
    tag = _dollar_quote_tag(body)
    assert tag == "$iknos2$"
    assert tag not in body
    assert sql.count(tag) == 2
    # The body's own $iknos$/$iknos1$ are inert text between the real $iknos2$ delimiters.
    assert f"{tag} {body} {tag}" in sql


def test_assembly_is_pure_and_needs_no_config() -> None:
    # Calling the seam directly (not cypher()) must not import the config singleton — that is
    # what keeps this boundary unit-testable in the env-free unit suite.
    a = _build_cypher_sql("iknos", "RETURN 1", _RETURNS)
    b = _build_cypher_sql("iknos", "RETURN 1", _RETURNS)
    assert a == b


# --- atomic_write: the W7 dual-write transaction discipline (commit/rollback bracket) ---


@pytest.mark.asyncio
async def test_atomic_write_commits_once_on_clean_exit() -> None:
    from unittest.mock import AsyncMock

    from iknos.db.age import atomic_write

    session = AsyncMock()
    async with atomic_write(session) as yielded:
        assert yielded is session  # yields the same session, just the commit/rollback bracket
    session.commit.assert_awaited_once()
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_atomic_write_rolls_back_and_reraises_on_failure() -> None:
    from unittest.mock import AsyncMock

    from iknos.db.age import atomic_write

    session = AsyncMock()

    class _BoomError(RuntimeError):
        pass

    with pytest.raises(_BoomError):
        async with atomic_write(session):
            await session.execute("first write")  # a write lands in the buffer ...
            raise _BoomError("second write failed")  # ... then a later write fails
    # The whole unit rolls back (no orphaned Action/vertex) and the error propagates.
    session.rollback.assert_awaited_once()
    session.commit.assert_not_called()
