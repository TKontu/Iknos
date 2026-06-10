"""Shared helpers for building LLM prompts from the structured vocabulary.

The proposition extractor (``core/proposition.py``) and the verifier
(``core/verify.py``) both interpolate enum vocabularies into their system prompts.
Keeping the helper here makes the no-drift guarantee literally single-source: a prompt's
legal-value list is generated from the same ``StrEnum`` the guided-decode schema is built
from, so the two can never disagree (a drift guided decoding would otherwise hide, since
the model is constrained to the schema's enum and a stale prompt just biases it silently).
"""

from enum import StrEnum


def vocab(enum: type[StrEnum]) -> str:
    """The legal value strings for an epistemic enum, joined for a prompt."""
    return " / ".join(e.value for e in enum)
