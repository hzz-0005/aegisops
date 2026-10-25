from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None
    fingerprint: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def required_labels(self) -> "AlertmanagerAlert":
        if not self.labels.get("alertname") or not self.labels.get("service"):
            raise ValueError("alertname and service labels are required")
        if self.startsAt.tzinfo is None or self.startsAt.utcoffset() is None:
            raise ValueError("startsAt must include a timezone")
        if self.endsAt is not None and (
            self.endsAt.tzinfo is None or self.endsAt.utcoffset() is None
        ):
            raise ValueError("endsAt must include a timezone")
        return self


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: Literal["4"]
    status: Literal["firing", "resolved"]
    alerts: list[AlertmanagerAlert] = Field(min_length=1, max_length=100)


class IngestResult(BaseModel):
    accepted: int
    duplicates: int


class AlertReceivedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["alert.received.v1"] = "alert.received.v1"
    message_id: UUID
    tenant_id: UUID
    alert_id: UUID


class IncidentCreatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["incident.created.v1"] = "incident.created.v1"
    message_id: UUID
    tenant_id: UUID
    incident_id: UUID


class IncidentUpdatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["incident.updated.v1"] = "incident.updated.v1"
    message_id: UUID
    tenant_id: UUID
    incident_id: UUID
    incident_version: int = Field(ge=1)


class IncidentSummary(BaseModel):
    id: UUID
    title: str
    status: str
    processing_phase: str
    severity: str
    version: int
    opened_at: datetime
    alert_count: int


class IncidentWorkflowView(BaseModel):
    workflow_id: str
    start_state: str
    run_id: str | None
    status: str
    review_state: str | None
    started_at: datetime | None
    closed_at: datetime | None


def nonblank_reason(value: str) -> str:
    reason = value.strip()
    if len(reason) < 3:
        raise ValueError("reason must contain at least 3 non-whitespace characters")
    return reason


class OperatorDecision(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


class OperatorWorkflowDecision(OperatorDecision):
    decision: Literal["ESCALATE", "CANCEL", "RESOLVE"]


class ReconcileWorkflowStartInput(BaseModel):
    expected_start_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


class MergeIncidentsInput(BaseModel):
    target_incident_id: UUID
    expected_source_version: int = Field(ge=1)
    expected_target_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


class SplitIncidentInput(BaseModel):
    expected_version: int = Field(ge=1)
    alert_ids: list[UUID] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


class IncidentCorrectionResult(BaseModel):
    incident_id: UUID
    alerts_moved: int


class IncidentRelationView(BaseModel):
    from_incident_id: UUID
    to_incident_id: UUID
    relation_type: str
    operator_id: str
    reason: str
    created_at: datetime


class TimelineItem(BaseModel):
    id: UUID
    event_type: str
    summary: str
    details: dict
    created_at: datetime


class CorrelationDecisionView(BaseModel):
    alert_id: UUID
    candidate_incident_id: UUID | None
    result_incident_id: UUID
    decision: str
    total_score: float
    matched_features: dict[str, float]
    conflicting_features: dict[str, float]
    rule_version: str
    decided_at: datetime


class DeploymentFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compose_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_id: str = Field(min_length=1, max_length=250)
    bad_deployment: bool


class DeploymentTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: DeploymentFingerprint
    after: DeploymentFingerprint


class ChangeEventInput(BaseModel):
    source: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=250)
    service: str = Field(min_length=1, max_length=120)
    environment: str = Field(min_length=1, max_length=80)
    change_type: str = Field(min_length=1, max_length=80)
    from_ref: str | None = Field(default=None, max_length=250)
    to_ref: str | None = Field(default=None, max_length=250)
    deployment: DeploymentTransition | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def timezone_required(self) -> "ChangeEventInput":
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        if self.deployment is not None:
            if self.change_type != "DEPLOYMENT":
                raise ValueError("deployment fingerprints require a DEPLOYMENT change")
            if (
                self.deployment.before.change_id != self.from_ref
                or self.deployment.after.change_id != self.to_ref
            ):
                raise ValueError("deployment fingerprints must match the release references")
            if self.deployment.before == self.deployment.after:
                raise ValueError("deployment fingerprints must describe a changed release")
        return self


class ChangeEventView(BaseModel):
    id: UUID
    source: str
    external_id: str
    service: str
    change_type: str
    occurred_at: datetime
