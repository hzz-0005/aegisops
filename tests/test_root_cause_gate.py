"""Mechanism×scope gate never grants root, current-fact or action authority."""

from datetime import timedelta
from uuid import uuid4

from aegisops.application.investigation.candidate_verifier import verify_candidates
from aegisops.application.investigation.root_cause_gate import evaluate_root_cause_gate
from tests.test_agent_loop import make_loop, tool_decision
from tests.test_candidate_verifier import fixture, record, verify


def run_gate(snap, trace, fresh=True):
    """Mirror the agent finish path: persist verifications on the trace, then gate."""
    trace.candidate_verifications = verify_candidates(snap, trace, fresh=fresh)
    gates = evaluate_root_cause_gate(trace)
    assert len(gates) == len(trace.candidate_verifications)
    return gates[0]


def membership_evidence(snap, alert, event_ids):
    """Direct-source membership over the full verified window; count == member list."""
    from aegisops.application.investigation.contracts import Evidence

    source = f"request-signal:{uuid4()}"
    alert["source_event_id"] = source
    return Evidence(
        source_type="ALERT", source_ref=source, kind="REQUEST_SIGNAL_MEMBERSHIP",
        subject="unclassified", polarity="CONTEXT",
        observed_at=alert["occurred_at"], collected_at=alert["occurred_at"],
        tool_name="request_signal_membership", tool_version="v1",
        payload={"alert_id": str(alert["id"]), "service": "order-service",
                 "resource": "inventory-service", "failure_code": "dependency_transport",
                 "member_event_ids": event_ids, "event_count": len(event_ids),
                 "window_start": alert["occurred_at"].isoformat(),
                 "window_end": (alert["occurred_at"] + timedelta(seconds=30)).isoformat(),
                 "alert_binding_status": "DIRECT_SOURCE_MEMBERSHIP",
                 "verification_basis": "PERSISTED_VERIFIED_WINDOW_MEMBERS",
                 "investigation_scope": {"tenant_id": str(snap["tenant_id"]),
                    "alert_id": str(alert["id"]), "target_service": "order-service",
                    "occurred_at": alert["occurred_at"].isoformat(),
                    "tool": "request_membership"}},
    )


def covered_fixture():
    """Verified REQUEST_FAILURE claim plus full direct-source membership."""
    snap, trace, fact = fixture()
    alert = snap["alerts"][0]
    second = fact.model_copy(deep=True, update={"id": uuid4()})
    second.payload["event_id"] = str(uuid4())
    second.payload["span_id"] = "c" * 16
    second.source_ref = f"request-event:{second.payload['event_id']}:{second.payload['span_id']}"
    record(trace, second)
    trace.hypotheses[0].supporting_evidence_ids = [fact.id, second.id]
    member = membership_evidence(snap, alert,
                                 [fact.payload["event_id"], second.payload["event_id"]])
    record(trace, member)
    check = verify(snap, trace)
    assert check.status == "OBSERVATION_VERIFIED"
    assert check.alert_binding_status == "DIRECT_SOURCE_MEMBERSHIP"
    assert check.verified_member_count == check.source_member_count == 2
    return snap, trace, check


def test_full_mechanism_and_coverage_passes_but_stays_unauthorized():
    snap, trace, _ = covered_fixture()
    gate = run_gate(snap, trace)
    assert gate.decision == "MECHANISM_SCOPE_SUPPORTED"
    assert gate.mechanism_status == "OBSERVATION_VERIFIED"
    assert gate.source_scope_status == "FULL_DIRECT_SOURCE"
    # Passing the gate still proves neither the root cause nor the current fault,
    # and it never makes an action eligible.
    assert gate.root_cause_status == "UNDETERMINED"
    assert gate.current_fault_status == "UNKNOWN"
    assert gate.action_eligible is False
    assert gate.blocking_reasons == ["CURRENT_FAULT_STATUS_UNKNOWN"]


def test_partial_coverage_blocks_gate():
    snap, trace, fact = fixture()
    alert = snap["alerts"][0]
    member = membership_evidence(snap, alert,
                                 [fact.payload["event_id"], str(uuid4())])
    record(trace, member)
    assert verify(snap, trace).verified_member_count == 1
    gate = run_gate(snap, trace)
    assert gate.decision == "GATE_NOT_PASSED"
    assert gate.source_scope_status == "PARTIAL_DIRECT_SOURCE"
    assert "SOURCE_COVERAGE_PARTIAL" in gate.blocking_reasons


def test_unbound_alert_blocks_gate():
    snap, trace, _ = fixture()
    gate = run_gate(snap, trace)
    assert gate.decision == "GATE_NOT_PASSED"
    assert gate.source_scope_status == "UNBOUND"
    assert "SOURCE_SCOPE_UNBOUND" in gate.blocking_reasons


def test_no_claim_blocks_gate():
    snap, trace, _ = fixture()
    trace.hypotheses[0].claim = None
    gate = run_gate(snap, trace)
    assert gate.mechanism_status == "UNSUPPORTED_CLAIM"
    assert gate.decision == "GATE_NOT_PASSED"
    assert "NO_VERIFIED_CLAIM" in gate.blocking_reasons


def test_conflicted_observations_block_gate():
    snap, trace, fact = fixture()
    conflict = fact.model_copy(deep=True, update={"id": uuid4(), "polarity": "CONFLICT"})
    record(trace, conflict)
    assert verify(snap, trace).status == "CONFLICTED"
    gate = run_gate(snap, trace)
    assert gate.mechanism_status == "CONFLICTED"
    assert gate.decision == "GATE_NOT_PASSED"
    assert "CONFLICTED_OBSERVATIONS" in gate.blocking_reasons


def test_stale_snapshot_blocks_gate():
    snap, trace, _ = covered_fixture()
    gate = run_gate(snap, trace, fresh=False)
    assert gate.mechanism_status == "INSUFFICIENT"
    assert gate.decision == "GATE_NOT_PASSED"
    assert "MECHANISM_UNPROVEN" in gate.blocking_reasons
    assert "CURRENT_FAULT_STATUS_UNKNOWN" in gate.blocking_reasons


def test_gate_is_pure_function_of_verifications():
    snap, trace, _ = covered_fixture()
    trace.candidate_verifications = verify_candidates(snap, trace, fresh=True)
    before = evaluate_root_cause_gate(trace)
    trace.hypotheses[0].label = "任意文本都不能改变门禁"
    trace.summary = "夸大结论"
    assert evaluate_root_cause_gate(trace) == before


def test_empty_trace_yields_no_verdicts():
    loop, _state = make_loop(tool_decision)
    assert evaluate_root_cause_gate(loop.trace) == []
