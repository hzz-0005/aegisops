"""Coverage and abstentions cannot be presented as root or action success."""
import pytest

from aegisops.application.fault_evaluation import evaluate_fault_sample


def alert(key, incident, status="INCONCLUSIVE", hypothesis=None, actions=None):
    return {"alert_id": key, "incident_id": incident, "investigation_status": status,
            "hypothesis": hypothesis, "plan_status": "PROPOSED" if actions else None,
            "action_types": actions}


def evaluate(alerts, roots):
    return evaluate_fault_sample({"alerts": alerts}, alert_roots=roots,
        compatible_domains={"dependency_unavailable"}, exact_hypotheses={"process_exit"},
        allowed_actions={"reviewed_restart"})


def test_generated_incident_ids_are_irrelevant_but_split_is_measured():
    a = evaluate([alert("a", "random-1"), alert("b", "random-2")], {"a": "root", "b": "root"})
    assert a["partition"]["false_split_pairs"] == 1
    assert a["wrong_proposal_rate"]["value"] is None
    assert a["exact_root_coverage"]["value"] == 0


def test_independent_roots_merged_into_one_incident_are_counted():
    a = evaluate([alert("a", "x"), alert("b", "x")], {"a": "first", "b": "second"})
    assert a["partition"]["false_merged_pairs"] == 1


def test_missing_incident_assignment_is_not_silently_scored_as_pass():
    a = evaluate([alert("a", None)], {"a": "root"})
    assert a["partition"] is None and a["unassigned_alert_ids"] == ["a"]


def test_ambiguous_symptom_is_excluded_with_visible_coverage():
    a = evaluate([alert("a", "x"), alert("b", "y")], {"a": "root"})
    assert a["oracle_coverage"]["value"] == 0.5
    assert a["unscored_alert_ids"] == ["b"]


def test_domain_only_does_not_become_exact_root_or_authorize_action():
    a = evaluate([alert("a", "x", "SUPPORTED", "dependency_unavailable", ["reviewed_restart"])],
                 {"a": "root"})
    assert a["verdict_counts"] == {"DOMAIN_ONLY": 1}
    assert a["exact_root_coverage"]["value"] == 0
    assert a["wrong_proposal_rate"]["value"] == 1


def test_exact_root_still_requires_an_allowed_action():
    a = evaluate([alert("a", "x", "SUPPORTED", "process_exit", ["delete_database"])],
                 {"a": "root"})
    assert a["exact_root_coverage"]["value"] == 1
    assert a["wrong_proposal_rate"]["value"] == 1


def test_unknown_cannot_improve_supported_claim_precision_denominator():
    a = evaluate([alert("a", "x", "SUPPORTED", "wrong_root"), alert("b", "y")],
                 {"a": "root", "b": "root"})
    assert a["wrong_supported_claim_rate"]["value"] == 1
    assert a["unknown_rate"]["value"] == 0.5


def test_mixed_root_incident_cannot_count_as_exact_root_success():
    a = evaluate([alert("a", "x", "SUPPORTED", "process_exit"),
                  alert("b", "x", "SUPPORTED", "process_exit")],
                 {"a": "first", "b": "second"})
    assert a["verdict_counts"] == {"UNSCORABLE_ORACLE": 1}
    assert a["exact_root_coverage"]["value"] is None
    assert a["root_judgment_coverage"]["value"] == 0


@pytest.mark.parametrize("alerts,roots", [([alert("a", "x"), alert("a", "x")], {"a": "r"}),
                                        ([alert("a", "x")], {"foreign": "r"})])
def test_invalid_oracle_or_duplicate_members_rejected(alerts, roots):
    with pytest.raises(ValueError):
        evaluate(alerts, roots)
