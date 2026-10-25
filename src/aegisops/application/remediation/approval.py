"""Independent approval bound to the exact action snapshot and current resource."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from aegisops.config import Profile, Settings
from aegisops.domain.models import ActionSnapshot, IncidentStatus
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionRecord,
    ApprovalRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    PolicyDecisionRecord,
    RemediationPlanRecord,
)

from .policy import _resource_version


class ApprovalRejected(ValueError):
    pass


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("reason must contain at least 3 non-whitespace characters")
        return value


class ApprovalView(BaseModel):
    id: UUID
    action_id: UUID
    status: str
    version: int
    snapshot_hash: str
    expires_at: datetime
    requested_by: str | None
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None


def get_approval(settings: Settings, tenant_id: UUID, approval_id: UUID) -> ApprovalView | None:
    with session_for(settings.database_url) as session:
        row = session.scalar(
            select(ApprovalRecord).where(
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.id == approval_id,
            )
        )
        return _view(row) if row is not None else None


def _view(row: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        id=row.id,
        action_id=row.action_id,
        status=row.status,
        version=row.version,
        snapshot_hash=row.snapshot_hash,
        expires_at=row.expires_at,
        requested_by=row.requested_by,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
        decided_at=row.decided_at,
    )


def decide_approval(
    settings: Settings,
    tenant_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionInput,
    approver_id: str,
) -> ApprovalView:
    with session_for(settings.database_url) as session, session.begin():
        approval = session.scalar(
            select(ApprovalRecord)
            .where(ApprovalRecord.tenant_id == tenant_id, ApprovalRecord.id == approval_id)
            .with_for_update()
        )
        if approval is None:
            raise ApprovalRejected("approval not found")
        if approval.version != body.expected_version:
            raise ApprovalRejected("approval version changed")
        if approval.status != "REQUESTED":
            raise ApprovalRejected("approval is no longer pending")
        if approval.requested_by is None or approval.policy_decision_id is None:
            raise ApprovalRejected("approval provenance is missing")
        if approval.requested_by == approver_id:
            raise ApprovalRejected("requester cannot approve or reject their own request")
        action = session.scalar(
            select(ActionRecord)
            .where(ActionRecord.tenant_id == tenant_id, ActionRecord.id == approval.action_id)
            .with_for_update()
        )
        if action is None:
            raise ApprovalRejected("action not found")
        plan = session.scalar(
            select(RemediationPlanRecord)
            .where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.id == action.plan_id,
            )
            .with_for_update()
        )
        incident = session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == action.incident_id)
            .with_for_update()
        )
        policy = session.scalar(
            select(PolicyDecisionRecord).where(
                PolicyDecisionRecord.tenant_id == tenant_id,
                PolicyDecisionRecord.id == approval.policy_decision_id,
            )
        )
        if plan is None or incident is None or policy is None:
            raise ApprovalRejected("linked governance records are missing")
        now = datetime.now(UTC)
        snapshot = ActionSnapshot.model_validate(action.snapshot)
        valid = (
            approval.expires_at > now
            and not settings.remediation_kill_switch
            and settings.profile == Profile.DEV
            and incident.status in {IncidentStatus.OPEN, IncidentStatus.MITIGATING}
            and incident.version == plan.source_incident_version
            and plan.status == "WAITING_APPROVAL"
            and action.status == "WAITING_APPROVAL"
            and plan.plan_hash == snapshot.plan_hash == policy.plan_hash
            and snapshot.digest() == approval.snapshot_hash == policy.snapshot_hash
            and policy.outcome == "REQUIRE_APPROVAL"
            and policy.action_id == action.id
            and policy.required_role == "Approver"
            and policy.required_approvals == 1
            and _resource_version(settings, tenant_id, snapshot.target) == snapshot.resource_version
        )
        if approval.expires_at <= now:
            new_status, action_status, plan_status = "EXPIRED", "BLOCKED", "BLOCKED"
        elif not valid:
            new_status, action_status, plan_status = "CANCELLED", "BLOCKED", "BLOCKED"
        elif body.decision == "REJECT":
            new_status, action_status, plan_status = "REJECTED", "DENIED", "REJECTED"
        else:
            new_status, action_status, plan_status = "APPROVED", "AUTHORIZED", "AUTHORIZED"
        approval.status = new_status
        approval.decided_by = approver_id
        approval.decision_reason = body.reason
        approval.decided_at = now
        approval.updated_at = now
        approval.version += 1
        action.status = action_status
        action.updated_at = now
        action.version += 1
        plan.status = plan_status
        plan.updated_at = now
        plan.version += 1
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=action.incident_id,
                event_type="APPROVAL_DECIDED",
                summary=f"Remediation approval {new_status}",
                details={
                    "approval_id": str(approval.id),
                    "action_id": str(action.id),
                    "policy_decision_id": str(policy.id),
                    "snapshot_hash": approval.snapshot_hash,
                    "operator_id": approver_id,
                    "reason": body.reason,
                    "old_version": body.expected_version,
                    "new_version": approval.version,
                    "status": new_status,
                },
            )
        )
        view = _view(approval)
    return view
