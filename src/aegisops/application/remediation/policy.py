"""Fail-closed OPA authorization and snapshot-bound approval request creation."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aegisops.config import Profile, Settings
from aegisops.domain.models import ActionSnapshot
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionRecord,
    ApprovalRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    PolicyDecisionRecord,
    RemediationPlanRecord,
    ResourceRecord,
    ServiceRecord,
)

from .service import PlanRejected, get_plan


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    reason: str = Field(max_length=500)
    policy_version: str = Field(max_length=80)
    required_role: str = ""
    required_approvals: int = Field(default=0, ge=0)


class PolicyDecisionView(BaseModel):
    id: UUID
    action_id: UUID
    outcome: str
    reason: str
    policy_version: str
    required_role: str | None
    required_approvals: int
    approval_id: UUID | None


def _resource_version(settings: Settings, tenant_id: UUID, target: str) -> int | None:
    try:
        kind, resource_id = target.split("/", 1)
        target_id = UUID(resource_id)
    except (ValueError, AttributeError):
        return None
    model = ResourceRecord if kind == "resource" else ServiceRecord if kind == "service" else None
    if model is None:
        return None
    with session_for(settings.database_url) as session:
        return session.scalar(
            select(model.version).where(
                model.tenant_id == tenant_id,
                model.id == target_id,
                model.environment == "lab",
            )
        )


def _query_opa(settings: Settings, policy_input: dict) -> PolicyResult:
    response = httpx.post(
        f"{settings.opa_url.rstrip('/')}/v1/data/aegisops/remediation/decision",
        json={"input": policy_input},
        timeout=3,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "result" not in data:
        raise ValueError("OPA returned no decision")
    return PolicyResult.model_validate(data["result"])


def authorize_plan(
    settings: Settings, tenant_id: UUID, incident_id: UUID, operator_id: str
) -> PolicyDecisionView:
    proposal = get_plan(settings, tenant_id, incident_id)
    if proposal is None or proposal.status != "PROPOSED" or len(proposal.actions) != 1:
        raise PlanRejected("one proposed plan with one action is required")
    action = proposal.actions[0]
    with session_for(settings.database_url) as session:
        incident = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
        if incident is None:
            raise PlanRejected("incident not found")
        incident_status, incident_version = incident.status, incident.version
    resource_version = _resource_version(settings, tenant_id, action.snapshot.target)
    policy_input = {
        "stage": "PLAN",
        "actor": {"id": operator_id, "role": "Operator"},
        "tenant": {"id": str(tenant_id)},
        "incident": {
            "id": str(incident_id),
            "status": incident_status,
            "version": incident_version,
        },
        "plan": {
            "id": str(proposal.id),
            "status": proposal.status,
            "source_incident_version": proposal.source_incident_version,
            "is_stale": proposal.is_stale,
            "plan_hash": proposal.plan_hash,
        },
        "action": {
            "id": str(action.id),
            "status": action.status,
            "snapshot": action.snapshot.model_dump(mode="json"),
        },
        "resource": {"target": action.snapshot.target, "version": resource_version},
        "environment": {
            "name": "lab" if settings.profile == Profile.DEV else settings.profile.value
        },
        "risk": {"level": action.snapshot.risk},
        "approval": {},
        "runtime": {"kill_switch": settings.remediation_kill_switch},
    }
    try:
        result = _query_opa(settings, policy_input)
    except (httpx.HTTPError, ValueError, TypeError):
        result = PolicyResult(
            outcome="DENY",
            reason="OPA unavailable or returned an invalid decision",
            policy_version="unavailable",
        )
    if result.outcome == "ALLOW":
        result = PolicyResult(
            outcome="DENY",
            reason="automatic remediation authorization is not enabled",
            policy_version=result.policy_version,
        )
    with session_for(settings.database_url) as session, session.begin():
        incident = session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id)
            .with_for_update()
        )
        plan = session.scalar(
            select(RemediationPlanRecord)
            .where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.id == proposal.id,
            )
            .with_for_update()
        )
        record = session.scalar(
            select(ActionRecord)
            .where(ActionRecord.tenant_id == tenant_id, ActionRecord.id == action.id)
            .with_for_update()
        )
        if incident is None or plan is None or record is None:
            raise PlanRejected("proposal disappeared during evaluation")
        stored_snapshot = ActionSnapshot.model_validate(record.snapshot)
        if (
            incident.version != incident_version
            or plan.status != "PROPOSED"
            or record.status != "PROPOSED"
            or plan.plan_hash != proposal.plan_hash
            or stored_snapshot.digest() != action.snapshot_hash
            or _resource_version(settings, tenant_id, stored_snapshot.target) != resource_version
        ):
            result = PolicyResult(
                outcome="DENY",
                reason="proposal or target changed during policy evaluation",
                policy_version=result.policy_version,
            )
        decision = PolicyDecisionRecord(
            tenant_id=tenant_id,
            action_id=action.id,
            plan_hash=proposal.plan_hash,
            snapshot_hash=action.snapshot_hash,
            policy_version=result.policy_version,
            outcome=result.outcome,
            reason=result.reason,
            required_role=result.required_role or None,
            required_approvals=result.required_approvals,
            input=policy_input,
        )
        session.add(decision)
        session.flush()
        approval_id = None
        if result.outcome == "REQUIRE_APPROVAL":
            plan.status = "WAITING_APPROVAL"
            record.status = "WAITING_APPROVAL"
            plan.updated_at = record.updated_at = datetime.now(UTC)
            plan.version += 1
            record.version += 1
            approval = ApprovalRecord(
                tenant_id=tenant_id,
                action_id=action.id,
                status="REQUESTED",
                snapshot_hash=action.snapshot_hash,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                policy_decision_id=decision.id,
                requested_by=operator_id,
            )
            session.add(approval)
            session.flush()
            approval_id = approval.id
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="POLICY_DECIDED",
                summary=f"Remediation policy: {result.outcome}",
                details={
                    "decision_id": str(decision.id),
                    "action_id": str(action.id),
                    "plan_hash": proposal.plan_hash,
                    "snapshot_hash": action.snapshot_hash,
                    "policy_version": result.policy_version,
                    "outcome": result.outcome,
                    "reason": result.reason,
                    "approval_id": str(approval_id) if approval_id else None,
                },
            )
        )
        decision_view = PolicyDecisionView(
            id=decision.id,
            action_id=action.id,
            outcome=result.outcome,
            reason=result.reason,
            policy_version=result.policy_version,
            required_role=result.required_role or None,
            required_approvals=result.required_approvals,
            approval_id=approval_id,
        )
    return decision_view
