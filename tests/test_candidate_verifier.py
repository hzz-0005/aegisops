"""Observation, provenance, coverage and conflict checks cannot grant root authority."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from aegisops.application.investigation.agent_contracts import (
    AgentHypothesis,
    AgentStep,
    ObservationClaim,
)
from aegisops.application.investigation.candidate_verifier import verify_candidates
from aegisops.application.investigation.contracts import Evidence
from tests.test_agent_loop import make_loop, tool_decision


def fixture():
    loop, state = make_loop(tool_decision)
    snap, trace = state["snapshot"], loop.trace
    alert = snap["alerts"][0]
    claim = ObservationClaim(kind="REQUEST_FAILURE", alert_id=alert["id"],
                             service="order-service", dependency="inventory-service",
                             failure_code="dependency_transport")
    event_id, span_id = str(uuid4()), "b" * 16
    evidence = Evidence(
        source_type="TRACE", source_ref=f"request-event:{event_id}:{span_id}",
        kind="VERIFIED_DEPENDENCY_REQUEST", subject="unclassified", polarity="CONTEXT",
        observed_at=alert["occurred_at"], collected_at=alert["occurred_at"],
        tool_name="request_events_and_tempo", tool_version="dependency-context-v1",
        payload={"service": claim.service, "dependency": claim.dependency,
                 "failure_code": claim.failure_code, "event_id": event_id,
                 "span_id": span_id, "trace_id": "a" * 32,
                 "provenance_status": "VERIFIED_SPAN_FACT", "alert_binding_status": "UNBOUND",
                 "investigation_scope": {"tenant_id": str(snap["tenant_id"]),
                    "alert_id": str(alert["id"]), "target_service": claim.service,
                    "occurred_at": alert["occurred_at"].isoformat(),
                    "tool": "dependency_requests"}},
    )
    trace.hypotheses = [AgentHypothesis(label="自由文本不能决定结果", state="CANDIDATE",
        rationale="独立核验引用", supporting_evidence_ids=[evidence.id], claim=claim)]
    record(trace, evidence)
    return snap, trace, evidence


def record(trace, evidence, status="COMPLETED"):
    trace.steps.append(AgentStep(number=len(trace.steps) + 1, phase="TOOL", status=status,
        input_hash="test", evidence_ids=[evidence.id], evidence=[evidence.model_dump(mode="json")]))


def verify(snap, trace, fresh=True):
    return verify_candidates(snap, trace, fresh=fresh)[0]


def test_exact_observation_does_not_prove_root_current_or_alert_binding():
    snap, trace, _ = fixture()
    check = verify(snap, trace)
    assert check.status == "OBSERVATION_VERIFIED"
    assert check.root_cause_status == "UNDETERMINED" and check.current_fault_status == "UNKNOWN"
    assert check.alert_binding_status == "UNBOUND" and check.source_member_count is None
    trace.hypotheses[0].label = "100%根因确定、立即修复"
    assert verify(snap, trace) == check


@pytest.mark.parametrize("mutation", ["tenant", "alert", "target", "time", "span",
                                     "event", "code", "service", "provenance", "tool"])
def test_invalid_identity_or_fact_is_not_verified(mutation):
    snap, trace, e = fixture()
    if mutation in {"tenant", "alert", "target", "time"}:
        key = {"tenant": "tenant_id", "alert": "alert_id", "target": "target_service",
               "time": "occurred_at"}[mutation]
        e.payload["investigation_scope"][key] = "foreign"
    else:
        key = {"span": "span_id", "event": "event_id", "code": "failure_code",
               "service": "service", "provenance": "provenance_status"}.get(mutation)
        if key:
            e.payload[key] = "foreign"
        else:
            e.tool_name = "model.supplied"
    trace.steps[0].evidence = [e.model_dump(mode="json")]
    assert verify(snap, trace).status == "INSUFFICIENT"


@pytest.mark.parametrize("status", ["FAILED", "RESERVED", "NO_DATA"])
def test_only_completed_backend_results_count(status):
    snap, trace, _ = fixture()
    trace.steps[0].status = status
    assert verify(snap, trace).status == "INSUFFICIENT"


def test_stale_or_unknown_citations_fail_closed():
    snap, trace, e = fixture()
    assert verify(snap, trace, False).reason_codes == ["SNAPSHOT_CHANGED"]
    trace.hypotheses[0].supporting_evidence_ids = [uuid4()]
    assert verify(snap, trace).status == "INSUFFICIENT"
    trace.hypotheses[0].supporting_evidence_ids = [e.id]
    e.observed_at -= timedelta(seconds=31)
    trace.steps[0].evidence = [e.model_dump(mode="json")]
    assert verify(snap, trace).status == "INSUFFICIENT"


def test_partial_direct_membership_preserves_full_denominator():
    snap, trace, e = fixture()
    alert = snap["alerts"][0]
    source = f"request-signal:{uuid4()}"
    alert["source_event_id"] = source
    m = e.model_copy(deep=True, update={"id": uuid4(), "source_type": "ALERT",
        "source_ref": source, "kind": "REQUEST_SIGNAL_MEMBERSHIP",
        "tool_name": "request_signal_membership"})
    m.payload.update({"alert_id": str(alert["id"]), "resource": "inventory-service",
        "member_event_ids": [e.payload["event_id"], str(uuid4())], "event_count": 2,
        "window_start": alert["occurred_at"].isoformat(),
        "window_end": (alert["occurred_at"] + timedelta(seconds=30)).isoformat(),
        "alert_binding_status": "DIRECT_SOURCE_MEMBERSHIP",
        "verification_basis": "PERSISTED_VERIFIED_WINDOW_MEMBERS"})
    record(trace, m)
    m.payload["investigation_scope"]["tool"] = "request_membership"
    trace.steps[-1].evidence = [m.model_dump(mode="json")]
    check = verify(snap, trace)
    assert check.alert_binding_status == "DIRECT_SOURCE_MEMBERSHIP"
    assert (check.verified_member_count, check.source_member_count) == (1, 2)
    assert "PARTIAL_SOURCE_COVERAGE" in check.reason_codes
    assert check.root_cause_status == "UNDETERMINED"
    m.payload["member_event_ids"] = [e.payload["event_id"]] * 2
    trace.steps[-1].evidence = [m.model_dump(mode="json")]
    assert verify(snap, trace).source_member_count is None


def test_unmentioned_runtime_counterexample_is_checked():
    snap, trace, e = fixture()
    claim = ObservationClaim(kind="RUNTIME_STATE", alert_id=snap["alerts"][0]["id"],
                             service="inventory-service", state="paused")
    trace.hypotheses[0].claim = claim
    e.source_type, e.kind = "METRIC", "RUNTIME_STATE_OBSERVATION"
    e.tool_name = "prometheus.query_range"
    e.payload.update({"service": claim.service, "state": "paused", "sample_count": 1})
    e.payload["investigation_scope"].update({
        "tool": "runtime_state", "target_service": claim.service})
    trace.steps[0].evidence = [e.model_dump(mode="json")]
    assert verify(snap, trace).status == "OBSERVATION_VERIFIED"
    conflict = e.model_copy(deep=True, update={"id": uuid4()})
    conflict.payload["state"] = "running"
    record(trace, conflict)
    check = verify(snap, trace)
    assert check.status == "CONFLICTED" and check.conflicting_evidence_ids == [conflict.id]


def test_different_requests_can_have_different_failure_codes_without_false_conflict():
    snap, trace, e = fixture()
    mixed = e.model_copy(deep=True, update={"id": uuid4()})
    mixed.payload["failure_code"] = "dependency_timeout"
    record(trace, mixed)
    assert verify(snap, trace).status == "OBSERVATION_VERIFIED"


def test_free_text_and_wrong_claim_shape_cannot_be_promoted():
    snap, trace, _ = fixture()
    trace.hypotheses[0].claim = None
    assert verify(snap, trace).status == "UNSUPPORTED_CLAIM"
    trace.hypotheses[0].claim = ObservationClaim(kind="REQUEST_FAILURE",
        alert_id=snap["alerts"][0]["id"], service="order-service", state="paused")
    assert verify(snap, trace).reason_codes == ["INVALID_CLAIM_SHAPE"]


def test_observed_registered_upstream_does_not_expand_query_authority():
    snap, trace, e = fixture()
    claim = trace.hypotheses[0].claim
    claim.service, claim.dependency, claim.failure_code = (
        "gateway", "order-service", "upstream_http_5xx")
    e.payload.update({"service": claim.service, "dependency": claim.dependency,
                      "failure_code": claim.failure_code})
    e.payload["investigation_scope"]["tool"] = "incoming_requests"
    trace.steps[0].evidence = [e.model_dump(mode="json")]
    assert verify(snap, trace).status == "OBSERVATION_VERIFIED"
    e.payload["investigation_scope"]["target_service"] = "inventory-service"
    trace.steps[0].evidence = [e.model_dump(mode="json")]
    assert verify(snap, trace).reason_codes == ["CLAIM_OUTSIDE_SCOPE"]


def test_alert_context_citation_does_not_block_claim_verification():
    snap, trace, e = fixture()
    alert_id = UUID(trace.initial_evidence[0]["id"])
    assert trace.initial_evidence[0]["kind"] == "NORMALIZED_ALERT"
    trace.hypotheses[0].supporting_evidence_ids = [e.id, alert_id]
    check = verify(snap, trace)
    assert check.status == "OBSERVATION_VERIFIED"
    assert check.supporting_evidence_ids == [e.id, alert_id]


def test_fabricated_citation_still_fails_closed():
    snap, trace, e = fixture()
    trace.hypotheses[0].supporting_evidence_ids = [e.id, uuid4()]
    check = verify(snap, trace)
    assert check.status == "INSUFFICIENT"
    assert "MISSING_COMPLETED_TOOL_CITATION" in check.reason_codes


def test_context_citation_without_claim_fact_cannot_verify():
    snap, trace, e = fixture()
    alert_id = UUID(trace.initial_evidence[0]["id"])
    trace.hypotheses[0].supporting_evidence_ids = [alert_id]
    check = verify(snap, trace)
    assert check.status == "INSUFFICIENT"
    assert "CITATION_DOES_NOT_MATCH_CLAIM" in check.reason_codes


def test_model_declared_context_conflict_does_not_block_claim():
    snap, trace, e = fixture()
    link = e.model_copy(deep=True, update={"id": uuid4(),
                                           "kind": "VERIFIED_REQUEST_FAILURE_LINK"})
    record(trace, link)
    trace.hypotheses[0].conflicting_evidence_ids = [link.id]
    check = verify(snap, trace)
    # A propagation fact the model labels "conflicting" is advisory only; the
    # claim stays verified because no backend CONFLICT polarity exists.
    assert check.status == "OBSERVATION_VERIFIED"
    assert link.id in check.conflicting_evidence_ids
    # Fabricated conflict ids are dropped, never trusted.
    trace.hypotheses[0].conflicting_evidence_ids = [uuid4()]
    check = verify(snap, trace)
    assert check.status == "OBSERVATION_VERIFIED"
    assert check.conflicting_evidence_ids == []
