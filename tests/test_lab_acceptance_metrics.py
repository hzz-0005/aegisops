"""Abstention and missing evaluations must not look like perfect root or action accuracy."""

from scripts.complete_lab_acceptance import fault_metrics


def case(status: str | None, hypothesis: str | None, plan: str | None) -> dict:
    return {
        "fault": "test", "mode": "pause", "detected": True,
        "detection_seconds_after_fault_activation": 12,
        "expected_fault_domains": ["dependency_unavailable"],
        "alerts": [{"incident_id": "one", "investigation_status": status,
                    "hypothesis": hypothesis, "plan_status": plan}],
    }


def test_abstention_is_not_perfect_root_or_action_accuracy() -> None:
    metrics = fault_metrics([case("INCONCLUSIVE", None, None)])
    assert metrics["abstention_rate"]["value"] == 1
    assert metrics["wrong_supported_domain_rate"]["value"] is None
    assert metrics["wrong_proposal_rate"]["value"] is None


def test_unsupported_proposal_is_counted_as_wrong() -> None:
    metrics = fault_metrics([case("INCONCLUSIVE", "dependency_unavailable", "PROPOSED")])
    assert metrics["wrong_proposal_rate"]["value"] == 1


def test_pending_is_not_counted_as_evaluated() -> None:
    metrics = fault_metrics([case(None, None, None)])
    assert metrics["pending_incident_count"] == 1
    assert metrics["wrong_supported_domain_per_judged_incident"]["value"] is None


def test_duplicate_alerts_do_not_inflate_incident_denominator() -> None:
    sample = case("SUPPORTED", "wrong-domain", "PROPOSED")
    sample["alerts"] *= 2
    metrics = fault_metrics([sample])
    assert metrics["wrong_supported_domain_rate"] == {"numerator": 1, "denominator": 1, "value": 1}
