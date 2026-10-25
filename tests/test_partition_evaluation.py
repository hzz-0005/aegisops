import pytest

from aegisops.application.partition_evaluation import evaluate_partition


def test_partition_ignores_group_order_and_member_order():
    report = evaluate_partition([["1", "2", "5"], ["3", "4"]],
                                [["4", "3"], ["5", "2", "1"]])
    assert report["exact_match"]
    assert report["correct_joined_pairs"] == 4
    assert report["correct_separate_pairs"] == 6
    assert report["false_merge_rate"] == report["false_split_rate"] == 0


def test_wrong_merge_and_split_are_counted_independently():
    report = evaluate_partition([["a", "b"], ["c", "d"]], [["a", "c"], ["b"], ["d"]])
    assert not report["exact_match"]
    assert report["false_merged_pairs"] == 1
    assert report["false_split_pairs"] == 2
    assert report["false_merge_rate"] == report["false_split_rate"] == 1
    assert report["correct_separate_pairs"] == 3


def test_no_joined_pairs_has_undefined_merge_rate():
    report = evaluate_partition([["a", "b"]], [["a"], ["b"]])
    assert report["false_merge_rate"] is None
    assert report["false_split_rate"] == 1


@pytest.mark.parametrize("actual", [[["a"]], [["a", "b", "foreign"]],
                                    [["a", "a"], ["b"]], [["a", "b"], []], ["ab"]])
def test_missing_duplicate_foreign_or_empty_groups_are_rejected(actual):
    with pytest.raises(ValueError):
        evaluate_partition([["a", "b"]], actual)
