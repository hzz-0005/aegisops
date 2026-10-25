"""Request grouping must preserve unknowns and separate root truth from propagation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aegisops.application.investigation.request_candidates import Anchor
from aegisops.application.investigation.request_shadow import shadow_partition
from aegisops.application.partition_evaluation import evaluate_partition
from aegisops.application.request_events import RequestFailureEventView
from aegisops.application.request_failure_links import RequestFailureLink


def sample():
    tenant, now = uuid4(), datetime.now(UTC)
    events = []
    proofs = []
    for i in range(4):
        trace = f"{i + 1:032x}"
        down = RequestFailureEventView(id=uuid4(), source_event_id=uuid4(), service="orders",
            trace_id=trace, span_id=f"{i * 2 + 1:016x}", failure_code="dependency_transport",
            resource="payments", occurred_at=now, provenance_status="SOURCE_ASSERTED")
        up = down.model_copy(update={"id": uuid4(), "source_event_id": uuid4(),
            "service": "gateway", "resource": "orders", "span_id": f"{i * 2 + 2:016x}",
            "failure_code": "upstream_http_5xx", "occurred_at": now + timedelta(seconds=1)})
        events.extend([up, down])
        proofs.append(RequestFailureLink(trace_id=trace, upstream_event_id=up.id,
            downstream_event_id=down.id, upstream_service="gateway", downstream_service="orders",
            downstream_failure_code=down.failure_code, downstream_resource=down.resource,
            span_path=[up.span_id, down.span_id], observed_at=up.occurred_at))
    anchors = [Anchor(tenant, uuid4(), 1, uuid4(), uuid4(),
        frozenset(e.id for e in events[::2])), Anchor(tenant, uuid4(), 1, uuid4(), uuid4(),
        frozenset(e.id for e in events[1::2]))]
    return {"tenant_id": tenant, "anchors": anchors, "events": events, "proofs": proofs}


def test_four_requests_with_two_shared_roots_remain_four_propagation_groups():
    case = sample()
    result = shadow_partition(**case)
    assert len(result["groups"]) == 4 and result["joined_member_count"] == 8
    actual = [g["members"] for g in result["groups"]]
    root_truth = [[str(e.id) for e in case["events"][:4]],
                  [str(e.id) for e in case["events"][4:]]]
    metric = evaluate_partition(root_truth, actual)
    assert metric["false_merged_pairs"] == 0 and metric["false_split_pairs"] == 8
    assert result["root_cause_status"] == "UNDETERMINED"
    assert not result["automatic_merge_allowed"]


def test_same_source_cohort_and_trace_without_paths_do_not_join():
    case = sample()
    case["events"][1].trace_id = case["events"][0].trace_id
    case["proofs"] = []
    result = shadow_partition(**case)
    assert len(result["groups"]) == 8 and result["joined_member_count"] == 0
    assert result["unresolved_member_count"] == 8


def test_missing_member_fact_remains_in_partition_and_denominator():
    case = sample()
    missing = case["events"].pop()
    case["proofs"] = case["proofs"][:-1]
    result = shadow_partition(**case)
    assert result["member_count"] == 8 and result["missing_fact_count"] == 1
    assert result["joined_member_count"] == 6
    assert any(g["members"] == [str(missing.id)] and g["status"] == "FACT_UNAVAILABLE"
               for g in result["groups"])


@pytest.mark.parametrize("mutation", ["trace", "service", "resource", "code", "span", "time",
                                     "foreign", "duplicate_path_span", "malformed_trace"])
def test_proof_identity_mismatch_quarantines_known_endpoints(mutation):
    case = sample()
    proof = case["proofs"][0]
    if mutation == "trace":
        proof.trace_id = "f" * 32
    elif mutation == "malformed_trace":
        proof.trace_id = "bad"
        case["events"][0].trace_id = case["events"][1].trace_id = "bad"
    elif mutation == "service":
        proof.downstream_service = "foreign"
    elif mutation == "resource":
        proof.downstream_resource = "foreign"
    elif mutation == "code":
        proof.downstream_failure_code = "foreign"
    elif mutation == "span":
        proof.span_path[-1] = "f" * 16
    elif mutation == "time":
        proof.observed_at += timedelta(seconds=1)
    elif mutation == "foreign":
        proof.downstream_event_id = uuid4()
    else:
        proof.span_path = [proof.span_path[0], proof.span_path[0], proof.span_path[-1]]
    result = shadow_partition(**case)
    assert result["joined_member_count"] == 6 and result["rejected_proof_count"] == 1
    assert result["ambiguous_member_count"] >= 1


def test_conflicting_parent_paths_do_not_pick_first_observation():
    case = sample()
    alternative = case["proofs"][0].model_copy(deep=True)
    alternative.span_path.insert(1, "f" * 16)
    case["proofs"].append(alternative)
    result = shadow_partition(**case)
    assert result["ambiguous_member_count"] == 2 and result["joined_member_count"] == 6
    case["proofs"].reverse()
    assert shadow_partition(**case) == result


def test_duplicate_and_reordered_observations_preserve_partition_and_coverage():
    case = sample()
    before = shadow_partition(**case)
    case["anchors"].reverse()
    case["events"].reverse()
    case["proofs"] = list(reversed(case["proofs"])) * 2
    assert shadow_partition(**case) == before


def test_tenant_mismatch_is_rejected_before_grouping():
    case = sample()
    case["tenant_id"] = uuid4()
    with pytest.raises(ValueError, match="scope"):
        shadow_partition(**case)


def test_foreign_fact_and_duplicate_identity_are_rejected():
    case = sample()
    case["events"].append(case["events"][0])
    with pytest.raises(ValueError):
        shadow_partition(**case)
    case["events"][-1] = case["events"][0].model_copy(update={"id": uuid4()})
    with pytest.raises(ValueError, match="membership"):
        shadow_partition(**case)


def test_ambiguous_branch_quarantines_whole_request_component():
    case = sample()
    original = case["events"][1]
    alternative = original.model_copy(update={"id": uuid4(), "span_id": "a" * 16})
    case["events"].append(alternative)
    selected = case["anchors"][1]
    case["anchors"][1] = Anchor(selected.tenant_id, selected.incident_id, 1,
        selected.alert_id, selected.signal_id, selected.members | {alternative.id})
    proof = case["proofs"][0].model_copy(deep=True)
    proof.downstream_event_id = alternative.id
    proof.span_path[-1] = alternative.span_id
    case["proofs"].append(proof)
    result = shadow_partition(**case)
    assert result["member_count"] == 9 and result["ambiguous_member_count"] == 3
    assert result["joined_member_count"] == 6 and result["joined_proof_count"] == 3


def test_missing_fact_proof_does_not_turn_source_assertion_into_verified_group():
    case = sample()
    case["events"].pop()
    result = shadow_partition(**case)
    assert result["member_count"] == 8 and result["missing_fact_count"] == 1
    assert result["joined_member_count"] == 6 and result["rejected_proof_count"] == 1
    assert result["ambiguous_member_count"] == 2


def test_large_member_universe_fails_before_partitioning():
    case = sample()
    selected = case["anchors"][0]
    case["anchors"][0] = Anchor(selected.tenant_id, selected.incident_id, 1,
        selected.alert_id, selected.signal_id, selected.members | {uuid4() for _ in range(1000)})
    with pytest.raises(ValueError, match="member budget"):
        shadow_partition(**case)


def test_overlapping_source_memberships_do_not_duplicate_request_members():
    case = sample()
    selected = case["anchors"][0]
    case["anchors"].append(Anchor(selected.tenant_id, uuid4(), 1, uuid4(), uuid4(),
                                  selected.members))
    result = shadow_partition(**case)
    assert result["overlapping_source_member_count"] == 4
    assert result["member_count"] == result["joined_member_count"] == 8
    assert len(result["groups"]) == 4
