import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegisops.domain.models import (
    Action,
    ActionSnapshot,
    Approval,
    ApprovalStatus,
    Incident,
    IncidentStatus,
    ProcessingPhase,
)


def test_incident_cannot_resolve_without_verification() -> None:
    incident = Incident(tenant_id=uuid4(), title="Redis unavailable")
    with pytest.raises(ValueError):
        incident.transition_status(IncidentStatus.RESOLVED, verification_passed=True)
    for phase in (
        ProcessingPhase.READY_FOR_INVESTIGATION,
        ProcessingPhase.INVESTIGATING,
        ProcessingPhase.PLANNING,
        ProcessingPhase.AUTHORIZING,
        ProcessingPhase.READY_TO_EXECUTE,
        ProcessingPhase.EXECUTING,
        ProcessingPhase.VERIFYING,
    ):
        incident = incident.transition_phase(phase)
    incident = incident.transition_status(IncidentStatus.MITIGATING)
    with pytest.raises(ValueError, match="telemetry evidence"):
        incident.transition_status(IncidentStatus.RESOLVED)
    resolved = incident.transition_status(IncidentStatus.RESOLVED, verification_passed=True)
    assert resolved.resolved_at is not None
    assert resolved.processing_phase == ProcessingPhase.NONE
    assert resolved.version == incident.version + 1
    closed = resolved.transition_status(IncidentStatus.CLOSED)
    assert closed.resolved_at == resolved.resolved_at


def test_operator_escalation_is_phase_not_incident_lifecycle() -> None:
    incident = Incident(tenant_id=uuid4(), title="Inventory unavailable")
    escalated = incident.transition_phase(ProcessingPhase.ESCALATED)
    assert escalated.status == IncidentStatus.OPEN
    assert escalated.processing_phase == ProcessingPhase.ESCALATED
    assert (
        escalated.transition_status(IncidentStatus.CANCELLED).processing_phase
        == ProcessingPhase.NONE
    )


def test_verified_recovery_transition_requires_investigating_incident() -> None:
    incident = Incident(tenant_id=uuid4(), title="Redis unavailable")
    with pytest.raises(ValueError):
        incident.begin_verified_resolution()
    ready = incident.transition_phase(ProcessingPhase.READY_FOR_INVESTIGATION)
    investigating = ready.transition_phase(ProcessingPhase.INVESTIGATING)
    verifying = investigating.begin_verified_resolution()
    assert verifying.status == IncidentStatus.MITIGATING
    assert verifying.processing_phase == ProcessingPhase.VERIFYING
    resolved = verifying.transition_status(IncidentStatus.RESOLVED, verification_passed=True)
    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.processing_phase == ProcessingPhase.NONE


def test_approval_is_bound_to_action_snapshot_tenant_and_expiry() -> None:
    tenant_id = uuid4()
    snapshot = ActionSnapshot(
        action_type="restart_cache",
        target="dev/cache/redis-main",
        parameters={"graceful": True},
        resource_version=4,
        plan_hash="plan-v1",
        risk="HIGH",
    )
    action = Action(tenant_id=tenant_id, incident_id=uuid4(), plan_id=uuid4(), snapshot=snapshot)
    now = datetime.now(UTC)
    approval = Approval(
        tenant_id=tenant_id,
        action_id=action.id,
        status=ApprovalStatus.APPROVED,
        snapshot_hash=snapshot.digest(),
        expires_at=now + timedelta(minutes=5),
    )
    assert approval.permits(action, at=now)
    assert not approval.permits(action, at=approval.expires_at)
    changed = action.model_copy(
        update={"snapshot": snapshot.model_copy(update={"resource_version": 5})}
    )
    assert not approval.permits(changed, at=now)
    assert not approval.permits(action.model_copy(update={"tenant_id": uuid4()}), at=now)


def test_legacy_snapshot_hash_survives_v2_schema_and_round_trip() -> None:
    legacy = {
        "action_type": "restart_cache",
        "target": f"resource/{uuid4()}",
        "parameters": {"graceful": True},
        "resource_version": 4,
        "plan_hash": "plan-v1",
        "risk": "HIGH",
    }
    expected = sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = ActionSnapshot.model_validate(legacy)
    assert snapshot.digest() == expected
    assert ActionSnapshot.model_validate(snapshot.model_dump(mode="json")).digest() == expected


def test_v2_snapshot_hash_binds_tenant_environment_and_platform_target() -> None:
    data = {
        "schema_version": 2,
        "action_type": "rollback_service",
        "target": f"service/{uuid4()}",
        "parameters": {"to_ref": "approved-release"},
        "resource_version": 4,
        "plan_hash": "plan-v2",
        "risk": "HIGH",
        "tenant_id": str(uuid4()),
        "environment": "production",
        "platform_target_id": "cluster-uid/workload-uid",
    }
    snapshot = ActionSnapshot.model_validate(data)
    assert (
        ActionSnapshot.model_validate(snapshot.model_dump(mode="json")).digest()
        == snapshot.digest()
    )
    for field, replacement in (
        ("tenant_id", str(uuid4())),
        ("environment", "staging"),
        ("platform_target_id", "another-cluster/workload-uid"),
    ):
        assert (
            ActionSnapshot.model_validate({**data, field: replacement}).digest()
            != snapshot.digest()
        )
    for missing in ("tenant_id", "environment", "platform_target_id"):
        with pytest.raises(ValidationError):
            ActionSnapshot.model_validate(
                {key: value for key, value in data.items() if key != missing}
            )
    with pytest.raises(ValidationError):
        ActionSnapshot.model_validate({**data, "target": "service/order-service"})
    with pytest.raises(ValidationError):
        ActionSnapshot.model_validate({**data, "schema_version": 1})
    with pytest.raises(ValueError):
        snapshot.model_copy(update={"schema_version": 1}).digest()


def test_rejects_unexpected_domain_fields() -> None:
    with pytest.raises(ValidationError):
        Incident.model_validate(
            {"tenant_id": str(uuid4()), "title": "x", "arbitrary_shell": "echo"}
        )
