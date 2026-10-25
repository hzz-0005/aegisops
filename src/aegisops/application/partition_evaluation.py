"""Compare complete incident member partitions, independent of generated incident IDs."""

from collections import Counter
from collections.abc import Sequence

MAX_MEMBERS = 1000


def _partition(groups: Sequence[Sequence[str]]) -> tuple[frozenset[frozenset[str]], dict[str, int]]:
    owners: dict[str, int] = {}
    canonical = set()
    for index, group in enumerate(groups):
        if isinstance(group, str) or not group:
            raise ValueError("partition groups must be nonempty member lists")
        for member in group:
            if not isinstance(member, str) or not member.strip() or member in owners:
                raise ValueError("partition members must be unique nonempty identities")
            owners[member] = index
            if len(owners) > MAX_MEMBERS:
                raise ValueError("partition member limit exceeded")
        canonical.add(frozenset(group))
    return frozenset(canonical), owners


def _pairs(count: int) -> int:
    return count * (count - 1) // 2


def evaluate_partition(expected: Sequence[Sequence[str]], actual: Sequence[Sequence[str]]) -> dict:
    expected_groups, expected_owner = _partition(expected)
    actual_groups, actual_owner = _partition(actual)
    if set(expected_owner) != set(actual_owner):
        raise ValueError("partitions must cover exactly the same complete member universe")
    intersection = Counter((expected_owner[member], actual_owner[member])
                           for member in expected_owner)
    true_pairs = sum(_pairs(count) for count in intersection.values())
    expected_pairs = sum(_pairs(len(group)) for group in expected_groups)
    actual_pairs = sum(_pairs(len(group)) for group in actual_groups)
    false_merge = actual_pairs - true_pairs
    false_split = expected_pairs - true_pairs
    return {
        "member_count": len(expected_owner), "expected_group_count": len(expected_groups),
        "actual_group_count": len(actual_groups), "exact_match": expected_groups == actual_groups,
        "correct_joined_pairs": true_pairs, "false_merged_pairs": false_merge,
        "false_split_pairs": false_split,
        "correct_separate_pairs": (
            _pairs(len(expected_owner)) - true_pairs - false_merge - false_split),
        "false_merge_rate": false_merge / actual_pairs if actual_pairs else None,
        "false_split_rate": false_split / expected_pairs if expected_pairs else None,
        "rate_basis": "false merge / predicted joined pairs; false split / expected joined pairs",
    }
