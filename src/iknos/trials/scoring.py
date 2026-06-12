"""Bias-controlled scoring: a fixed, content-hash-seeded permutation schedule (Trial A0 / V3).

Evaluation must be **bias-controlled** — gold answers presented under a controlled, replayable
ordering so no metric depends on presentation order, and never LLM-as-judge (architecture.md
§8, §13). When a trial presents an ordered list to anything order-sensitive (a model under
test, an annotator, a scorer), it must shuffle the list so position cannot leak the answer, yet
keep the shuffle **replayable** so a run can be re-scored identically and audited (§10).

This module supplies exactly that schedule. It reuses the content-addressed Fisher–Yates
pattern of ``core/edge_judge.py::_permutation`` — a SHA-256 digest of a string key drives the
shuffle, so the order is a deterministic function of the key (no RNG seed, no process salt, no
``random`` import) and identical across runs and machines. It is reimplemented here rather than
imported because ``core/edge_judge`` imports ``core/llm``, and this package must not (the V3
import boundary).

Pure standard library; no model, no DB, no network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def content_permutation(key: str, count: int) -> list[int]:
    """A deterministic permutation of ``range(count)`` seeded by ``key`` — the §8 ordering guard.

    Content-addressed on ``key`` via SHA-256 (mirrors ``core/edge_judge._permutation``): the
    order is replayable across runs and machines, with no RNG-seed portability assumptions, yet
    differs per key so two questions are not presented in the same order. A Fisher–Yates shuffle
    consumes successive 4-byte windows of the digest, re-hashing with a counter when a long list
    needs more bytes.
    """
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    order = list(range(count))
    if count < 2:
        return order
    pool = bytearray(bytes.fromhex(_sha256_hex(key)))
    cursor = 0
    counter = 0
    for i in range(count - 1, 0, -1):
        if cursor + 4 > len(pool):
            counter += 1
            pool.extend(bytes.fromhex(_sha256_hex(f"{key}|{counter}")))
        word = int.from_bytes(pool[cursor : cursor + 4], "big")
        cursor += 4
        j = word % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


def inverse_permutation(order: Sequence[int]) -> list[int]:
    """The inverse of ``order``: ``inv[order[p]] == p`` for every slot ``p``.

    With ``order`` from :func:`content_permutation`, presentation slot ``p`` shows original
    index ``order[p]``. The inverse answers the reverse question — *which slot did original
    index ``o`` end up in* — as ``inverse_permutation(order)[o]``. Useful when a trial holds
    results keyed by original item and needs to know where each appeared.
    """
    inv = [0] * len(order)
    for permuted_slot, original_index in enumerate(order):
        inv[original_index] = permuted_slot
    return inv


@dataclass(frozen=True)
class PermutationSchedule:
    """A replayable family of per-key permutations, namespaced by ``salt``.

    Two trials (or two presentation passes within one trial) that must shuffle independently
    pass different salts; within one salt the order is a pure function of the item key, so the
    whole schedule is reconstructable from ``(salt, key)`` alone — nothing is stored. This is
    the unit a trial holds: ``schedule.order(question_id, n_options)`` and
    ``schedule.permute(options, question_id)``.
    """

    salt: str

    def _key(self, key: str) -> str:
        return f"{self.salt}|{key}"

    def order(self, key: str, count: int) -> list[int]:
        """The permutation of ``range(count)`` for ``key`` under this schedule's salt."""
        return content_permutation(self._key(key), count)

    def permute(self, items: Sequence[T], key: str) -> list[T]:
        """Return ``items`` reordered by this schedule's permutation for ``key``.

        ``result[p] = items[order[p]]`` — the item shown at presentation slot ``p``. Use
        :meth:`order` to recover which original index each slot held when scoring.
        """
        order = self.order(key, len(items))
        return [items[i] for i in order]
