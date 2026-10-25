"""Agent-path remediation: only gate-passed, mapped claims may be proposed."""

from uuid import uuid4

import pytest

from aegisops.application.investigation.agent_contracts import (
    ClaimVerification,
    ObservationClaim,
    RootCauseGateVerdict,
)
from aegisops.application.investigation.contracts import InvestigationReport
from aegisops.application.remediation.agent_mapping import (
    agent_action_candidate,
    map_claim,
)
from aegisops.application.remediation.catalog import load_remediation_catalog
from aegisops.application.remediation.service import PlanRejected, propose_plan
from aegisops.config import Settings

REDIS_CLAIM = ObservationClaim(
    kind="REQUEST_FAILURE", alert_id=uuid4(), service="payment-service",
    dependency="redis-main", failure_code="dependency_transport")


def verification(claim, index=1, status="OBSERVATION_VERIFIED"):
    return ClaimVerification(candidate_index=index, claim=claim, status=status,
                             supporting_evidence_ids=[uuid4()])


def gate(index=1, decision="MECHANISM_SCOPE_SUPPORTED"):
    return RootCauseGateVerdict(candidate_index=index, decision=decision,
                                mechanism_status="OBSERVATION_VERIFIED",
                                source_scope_status="FULL_DIRECT_SOURCE")


def report(verifications, gates, graph="readonly-agent-v3-mechanism-gate"):
    return InvestigationReport(
        tenant_id=uuid4(), incident_id=uuid4(), source_incident_version=3,
        current_incident_version=3, status="INCONCLUSIVE", graph_version=graph,
        summary="自主调查总结", primary_hypothesis=None, confidence=0,
        hypotheses=[], evidence=[], limitations=[], completed_at=__import__(
            "datetime").datetime.now(__import__("datetime").UTC),
        candidate_verifications=verifications, root_cause_gates=gates)


def test_redis_claim_maps_to_restart_cache_template():
    template = map_claim(REDIS_CLAIM)
    assert template is not None
    assert template.action_type == "restart_cache"
    assert template.target_slug == "redis-main"


def test_unmapped_or_malformed_claims_map_to_nothing():
    catalog = load_remediation_catalog()
    assert map_claim(None, catalog) is None
    assert map_claim(ObservationClaim(
        kind="REQUEST_FAILURE", alert_id=uuid4(), service="order-service",
        dependency="inventory-service", failure_code="dependency_transport"),
        catalog) is None
    assert map_claim(ObservationClaim(
        kind="REQUEST_FAILURE", alert_id=uuid4(), service="payment-service",
        dependency="redis-main", failure_code="upstream_http_5xx"), catalog) is None
    assert map_claim(ObservationClaim(
        kind="RUNTIME_STATE", alert_id=uuid4(), service="payment-service",
        state="paused"), catalog) is None


def test_candidate_requires_gate_and_verification_together():
    mapped = report([verification(REDIS_CLAIM)], [gate()])
    assert agent_action_candidate(mapped) is not None
    # Verified claim but the gate did not pass: never mappable.
    blocked = report([verification(REDIS_CLAIM)],
                     [gate(decision="GATE_NOT_PASSED")])
    assert agent_action_candidate(blocked) is None
    # Gate passed on the candidate but verification conflicted: never mappable.
    conflicted = report([verification(REDIS_CLAIM, status="CONFLICTED")], [gate()])
    assert agent_action_candidate(conflicted) is None
    # Neither verification nor gate: nothing.
    assert agent_action_candidate(report([], [])) is None


def test_deterministic_reports_are_untouched_by_agent_branch():
    from datetime import UTC, datetime

    supported = InvestigationReport(
        tenant_id=uuid4(), incident_id=uuid4(), source_incident_version=2,
        status="SUPPORTED", graph_version="readonly-investigation-v10-database-ancestry",
        summary="确定性结论", primary_hypothesis="redis_unavailable", confidence=0.9,
        hypotheses=[], evidence=[], limitations=[], completed_at=datetime.now(UTC))
    assert agent_action_candidate(supported) is None


def test_agent_proposal_rejects_without_gate_or_mapping():
    settings = Settings()

    def run_case(rep):
        import aegisops.application.remediation.service as service_module
        original = service_module.latest_report
        service_module.latest_report = lambda *args, **kwargs: rep
        try:
            return propose_plan(settings, uuid4(), uuid4())
        finally:
            service_module.latest_report = original

    # Gate not passed → rejected even though the claim would map.
    with pytest.raises(PlanRejected, match="gate-supported"):
        run_case(report([verification(REDIS_CLAIM)],
                        [gate(decision="GATE_NOT_PASSED")]))
    # No structured claim at all → rejected.
    with pytest.raises(PlanRejected, match="gate-supported"):
        run_case(report([verification(None)], [gate()]))
    # An agent report that somehow claimed SUPPORTED authority → rejected.
    broken = report([verification(REDIS_CLAIM)], [gate()])
    broken.primary_hypothesis = "redis_unavailable"
    with pytest.raises(PlanRejected, match="inconclusive"):
        run_case(broken)
