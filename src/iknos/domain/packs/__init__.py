"""Bundled domain packs (declared as JSON data; validated on load).

Trivial reference packs shipped with the codebase, primarily to prove the
declare → validate → persist path end-to-end (the G0.7 exit criterion) and to
give Phase 1 something concrete to anchor against.
"""

from pathlib import Path

from iknos.domain.pack import DomainPack

PACKS_DIR = Path(__file__).parent


def bundled_pack(name: str) -> DomainPack:
    """Load a bundled pack by file stem, e.g. ``bundled_pack("pump_basic")``."""
    return DomainPack.from_file(PACKS_DIR / f"{name}.json")
