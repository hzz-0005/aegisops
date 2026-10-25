"""Domain facts and deterministic transition rules; no framework I/O belongs here."""

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now_utc() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Entity(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    version: int = Field(default=1, ge=1)


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class ProcessingPhase(StrEnum):
    CORRELATING = "CORRELATING"
    READY_FOR_INVESTIGATION = "READY_FOR_INVESTIGATION"
    INVESTIGATING = "INVESTIGATING"
    PLANNING = "PLANNING"
    AUTHORIZING = "AUTHORIZING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY_TO_EXECUTE = "READY_TO_EXECUTE"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    ESCALATED = "ESCALATED"
    NONE = "NONE"


STATUS_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset(
        {IncidentStatus.MITIGATING, IncidentStatus.CANCELLED, IncidentStatus.SUPERSEDED}
    ),
    IncidentStatus.MITIGATING: frozenset(
        {IncidentStatus.OPEN, IncidentStatus.RESOLVED, IncidentStatus.CANCELLED}
    ),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED, IncidentStatus.OPEN}),
}

PHASE_TRANSITIONS: dict[ProcessingPhase, frozenset[ProcessingPhase]] = {
    ProcessingPhase.CORRELATING: frozenset({ProcessingPhase.READY_FOR_INVESTIGATION}),
    ProcessingPhase.READY_FOR_INVESTIGATION: frozenset({ProcessingPhase.INVESTIGATING}),
    ProcessingPhase.INVESTIGATING: frozenset({ProcessingPhase.PLANNING}),
    ProcessingPhase.PLANNING: frozenset({ProcessingPhase.AUTHORIZING}),
    ProcessingPhase.AUTHORIZING: frozenset(
        {ProcessingPhase.WAITING_APPROVAL, ProcessingPhase.READY_TO_EXECUTE}
    ),
    ProcessingPhase.WAITING_APPROVAL: frozenset({ProcessingPhase.READY_TO_EXECUTE}),
    ProcessingPhase.READY_TO_EXECUTE: frozenset({ProcessingPhase.EXECUTING}),
    ProcessingPhase.EXECUTING: frozenset({ProcessingPhase.VERIFYING}),
    ProcessingPhase.VERIFYING: frozenset({ProcessingPhase.INVESTIGATING, ProcessingPhase.NONE}),
    ProcessingPhase.ESCALATED: frozenset({ProcessingPhase.READY_FOR_INVESTIGATION}),
}


class Incident(Entity):
    title: str = Field(min_length=1, max_length=300)
    status: IncidentStatus = IncidentStatus.OPEN
    processing_phase: ProcessingPhase = ProcessingPhase.CORRELATING
    severity: str = "P3"
    correlation_key: str | None = None
    primary_service_id: UUID | None = None
    opened_at: datetime = Field(default_factory=now_utc)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def resolution_matches_status(self) -> "Incident":
        if (self.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}) != (
            self.resolved_at is not None
        ):
            raise ValueError(
                "resolved_at must be present exactly when status is RESOLVED or CLOSED"
            )
        if (
            self.status
            in {IncidentStatus.CLOSED, IncidentStatus.CANCELLED, IncidentStatus.SUPERSEDED}
            and self.processing_phase != ProcessingPhase.NONE
        ):
            raise ValueError("terminal incident must have NONE processing phase")
        return self

    def transition_status(
        self, target: IncidentStatus, *, verification_passed: bool = False
    ) -> "Incident":
        if target not in STATUS_TRANSITIONS.get(self.status, frozenset()):
            raise ValueError(f"invalid incident status transition: {self.status} -> {target}")
        if target == IncidentStatus.MITIGATING and self.processing_phase not in {
            ProcessingPhase.EXECUTING,
            ProcessingPhase.VERIFYING,
        }:
            raise ValueError("incident can mitigate only during execution or verification")
        if target == IncidentStatus.RESOLVED and (
            not verification_passed or self.processing_phase != ProcessingPhase.VERIFYING
        ):
            raise ValueError("verification phase and telemetry evidence are required to resolve")
        timestamp = now_utc()
        return self.model_copy(
            update={
                "status": target,
                "resolved_at": timestamp
                if target == IncidentStatus.RESOLVED
                else (self.resolved_at if target == IncidentStatus.CLOSED else None),
                "processing_phase": ProcessingPhase.NONE
                if target
                in {
                    IncidentStatus.RESOLVED,
                    IncidentStatus.CLOSED,
                    IncidentStatus.CANCELLED,
                    IncidentStatus.SUPERSEDED,
                }
                else (
                    ProcessingPhase.READY_FOR_INVESTIGATION
                    if self.status == IncidentStatus.RESOLVED and target == IncidentStatus.OPEN
                    else self.processing_phase
                ),
                "updated_at": timestamp,
                "version": self.version + 1,
            }
        )

    def transition_phase(self, target: ProcessingPhase) -> "Incident":
        if self.status not in {IncidentStatus.OPEN, IncidentStatus.MITIGATING}:
            raise ValueError("incident is not active")
        if target == self.processing_phase or (
            target not in PHASE_TRANSITIONS.get(self.processing_phase, frozenset())
            and target != ProcessingPhase.ESCALATED
        ):
            raise ValueError(
                f"invalid processing phase transition: {self.processing_phase} -> {target}"
            )
        timestamp = now_utc()
        return self.model_copy(
            update={
                "processing_phase": target,
                "updated_at": timestamp,
                "version": self.version + 1,
            }
        )

    def begin_verified_resolution(self) -> "Incident":
        """A completed action and recovery evidence are checked by the application service."""
        if (
            self.status != IncidentStatus.OPEN
            or self.processing_phase != ProcessingPhase.INVESTIGATING
        ):
            raise ValueError("only an open investigating incident can enter verified resolution")
        timestamp = now_utc()
        return self.model_copy(
            update={
                "status": IncidentStatus.MITIGATING,
                "processing_phase": ProcessingPhase.VERIFYING,
                "updated_at": timestamp,
                "version": self.version + 1,
            }
        )


class AgentRun(Entity):
    incident_id: UUID
    thread_id: UUID
    status: str = "PENDING"
    graph_version: str
    prompt_version: str
    model_provider: str
    model_name: str
    max_llm_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def thread_matches_run(self) -> "AgentRun":
        if self.thread_id != self.id:
            raise ValueError("thread_id must equal agent_run id")
        return self


class RemediationPlan(Entity):
    incident_id: UUID
    agent_run_id: UUID
    status: str = "PROPOSED"
    rationale: str = Field(min_length=1)


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    AUTHORIZED = "AUTHORIZED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY = "READY"
    ACQUIRING_LEASE = "ACQUIRING_LEASE"
    PRECHECKING = "PRECHECKING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    FAILED = "FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    CANCELLED = "CANCELLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RECOVERY_OBSERVED = "RECOVERY_OBSERVED"


class ActionSnapshot(StrictModel):
    schema_version: Literal[1, 2] = 1
    action_type: str = Field(min_length=1)
    target: str = Field(min_length=1)
    parameters: dict[str, str | int | float | bool | None]
    resource_version: int = Field(ge=0)
    plan_hash: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    tenant_id: UUID | None = None
    environment: str | None = Field(default=None, min_length=1, max_length=80)
    platform_target_id: str | None = Field(default=None, min_length=1, max_length=250)

    @model_validator(mode="after")
    def validate_version_scope(self) -> "ActionSnapshot":
        self._check_version_scope()
        return self

    def _check_version_scope(self) -> None:
        scoped_fields = (self.tenant_id, self.environment, self.platform_target_id)
        if self.schema_version == 1:
            if any(value is not None for value in scoped_fields):
                raise ValueError("v1 snapshot cannot contain production target identity")
        else:
            kind, separator, target_id = self.target.partition("/")
            if (
                any(value is None for value in scoped_fields)
                or not self.environment
                or self.environment.strip() != self.environment
                or not self.platform_target_id
                or self.platform_target_id.strip() != self.platform_target_id
                or kind not in {"service", "resource"}
                or separator != "/"
            ):
                raise ValueError("v2 snapshot requires a scoped managed target")
            try:
                UUID(target_id)
            except ValueError as exc:
                raise ValueError("v2 snapshot target must contain a catalog UUID") from exc

    def digest(self) -> str:
        self._check_version_scope()
        data = self.model_dump(mode="json")
        if self.schema_version == 1:
            for field in ("schema_version", "tenant_id", "environment", "platform_target_id"):
                data.pop(field)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


class Action(Entity):
    incident_id: UUID
    plan_id: UUID
    status: ActionStatus = ActionStatus.PROPOSED
    snapshot: ActionSnapshot


class ApprovalStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class Approval(Entity):
    action_id: UUID
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    snapshot_hash: str = Field(min_length=64, max_length=64)
    expires_at: datetime

    def permits(self, action: Action, *, at: datetime) -> bool:
        return (
            self.tenant_id == action.tenant_id
            and self.action_id == action.id
            and self.status == ApprovalStatus.APPROVED
            and at < self.expires_at
            and self.snapshot_hash == action.snapshot.digest()
        )
