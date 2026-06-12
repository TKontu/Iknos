"""The bias-controlled permutation schedule (``iknos.trials.scoring``).

Proves the two properties the §8 ordering guard needs: the order is a genuine permutation, and
it is **deterministic** in the content key (replayable across runs — the audit requirement),
yet varies by key. Also exercises the long-list rehash branch and the inverse mapping.
"""

from __future__ import annotations

from iknos.trials.scoring import (
    PermutationSchedule,
    content_permutation,
    inverse_permutation,
)


def test_permutation_is_deterministic() -> None:
    assert content_permutation("q42", 8) == content_permutation("q42", 8)


def test_permutation_is_a_bijection() -> None:
    order = content_permutation("q42", 8)
    assert sorted(order) == list(range(8))


def test_permutation_trivial_sizes() -> None:
    assert content_permutation("k", 0) == []
    assert content_permutation("k", 1) == [0]


def test_permutation_long_list_rehash_branch() -> None:
    # 50 elements need 49*4 = 196 bytes > one 32-byte digest, exercising the re-hash extension.
    order = content_permutation("long", 50)
    assert sorted(order) == list(range(50))


def test_permutation_varies_by_key() -> None:
    orders = {tuple(content_permutation(f"q{i}", 6)) for i in range(20)}
    assert len(orders) > 1, "permutation does not depend on the key"


def test_inverse_permutation_roundtrip() -> None:
    order = content_permutation("q7", 9)
    inv = inverse_permutation(order)
    # Original index o ends up at slot inv[o]; the slot holds order[slot] == o.
    for o in range(9):
        assert order[inv[o]] == o


def test_schedule_permute_matches_order() -> None:
    schedule = PermutationSchedule(salt="A1")
    items = ["opt0", "opt1", "opt2", "opt3"]
    order = schedule.order("question-1", len(items))
    assert schedule.permute(items, "question-1") == [items[i] for i in order]


def test_schedule_permute_preserves_multiset() -> None:
    schedule = PermutationSchedule(salt="A1")
    items = ["a", "b", "c", "d", "e"]
    assert sorted(schedule.permute(items, "q")) == sorted(items)


def test_schedule_salt_namespaces_orders() -> None:
    a = PermutationSchedule(salt="trialA")
    b = PermutationSchedule(salt="trialB")
    # Same key, different salt: independent schedules (different for at least one of several keys).
    differs = any(a.order(f"q{i}", 6) != b.order(f"q{i}", 6) for i in range(20))
    assert differs


def test_schedule_is_replayable_from_salt_and_key() -> None:
    # Nothing is stored: a fresh schedule with the same salt reproduces the order exactly.
    assert PermutationSchedule("s").order("q", 7) == PermutationSchedule("s").order("q", 7)
