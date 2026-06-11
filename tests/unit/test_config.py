"""Unit tests for the config settings (V11; §6.1 cost/identity plumbing).

``config.py`` is the env→Settings boundary. Two properties matter and had no direct test:
defaulting (a field absent from the environment falls back to its declared default, so most
code/tests need no env) and the **graph-name guard** (M1/V11) — ``graph_name`` is the one
value interpolated straight into the ``cypher()`` SQL, so it must be a bare SQL identifier.

Constructing :class:`Settings` is connection-free (it stores the URL string; nothing dials
the DB), which is why importing the config singleton is safe in env-free code. We pin that
too. ``_env_file=None`` keeps every case hermetic — no ambient ``.env`` can leak in.
"""

import os

import pytest
from pydantic import ValidationError

# settings = Settings() runs at module import and DATABASE_URL is required; set a harmless
# fake first (setdefault never clobbers a real one) so importing the module here cannot fail.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

from iknos.config import Settings, settings  # noqa: E402

_DB_URL = "postgresql+asyncpg://u:p@localhost:5432/iknos"

# Optional fields whose defaults a "minimal env" test must not see overridden by ambient env.
_OPTIONAL_ALIASES = ("API_HOST", "API_PORT", "LOG_LEVEL", "GRAPH_NAME")


def _minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _DB_URL)
    for alias in _OPTIONAL_ALIASES:
        monkeypatch.delenv(alias, raising=False)


def test_module_singleton_is_a_settings_instance() -> None:
    # Importing the module constructed the singleton without a live DB (no DB on import).
    assert isinstance(settings, Settings)
    assert isinstance(settings.database_url, str)


def test_defaults_apply_when_only_database_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.database_url == _DB_URL
    assert s.api_host == "0.0.0.0"
    assert s.api_port == 8000
    assert s.log_level == "INFO"
    assert s.graph_name == "iknos"


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_env(monkeypatch)
    monkeypatch.setenv("GRAPH_NAME", "custom_graph")
    monkeypatch.setenv("API_PORT", "9001")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)
    assert s.graph_name == "custom_graph"
    assert s.api_port == 9001
    assert s.log_level == "DEBUG"


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_construction_does_not_dial_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unreachable URL still constructs — Settings stores the string, it does not connect.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody@198.51.100.1:5432/none")
    s = Settings(_env_file=None)
    assert s.database_url.endswith("/none")


def test_graph_name_accepts_bare_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_env(monkeypatch)
    for name in ("iknos", "iknos_v2", "_private", "G1", "a" * 63):
        monkeypatch.setenv("GRAPH_NAME", name)
        assert Settings(_env_file=None).graph_name == name


@pytest.mark.parametrize(
    "bad",
    [
        "bad graph",  # space
        "graph;DROP TABLE actions",  # statement break
        "iknos') AS x; --",  # injection-shaped
        "graph-name",  # hyphen
        "1graph",  # leading digit
        "",  # empty
        "a" * 64,  # over the 63-char identifier limit
    ],
)
def test_graph_name_rejects_non_identifiers(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    _minimal_env(monkeypatch)
    monkeypatch.setenv("GRAPH_NAME", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
