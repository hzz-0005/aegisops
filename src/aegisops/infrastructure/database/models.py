"""Persistence mappings are deliberately separate from domain models."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantRecord(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)


class CoreRecord:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)


class IncidentRecord(CoreRecord, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_incidents_tenant_id"),
        Index("ix_incidents_tenant_status", "tenant_id", "status"),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    processing_phase: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(2), nullable=False)
    correlation_key: Mapped[str | None] = mapped_column(String(300))
    primary_service_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentWorkflowStartRecord(CoreRecord, Base):
    __tablename__ = "incident_workflow_starts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_workflow_starts_incident",
        ),
        UniqueConstraint("tenant_id", "incident_id", name="uq_incident_workflow_start"),
        UniqueConstraint("workflow_id", name="uq_incident_workflow_id"),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    first_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[str | None] = mapped_column(String(80))


class InvestigationReportRecord(CoreRecord, Base):
    __tablename__ = "investigation_reports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_investigation_reports_incident",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_investigation_reports_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "incident_id",
            "source_incident_version",
            name="uq_investigation_report_incident_version",
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_incident_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    primary_hypothesis: Mapped[str | None] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    hypotheses: Mapped[list] = mapped_column(JSONB, nullable=False)
    limitations: Mapped[list] = mapped_column(JSONB, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agent_trace: Mapped[dict | None] = mapped_column(JSONB)


class InvestigationAgentRunRecord(CoreRecord, Base):
    __tablename__ = "investigation_agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"], ["incidents.tenant_id", "incidents.id"],
            name="fk_agent_runs_incident",
        ),
        UniqueConstraint("tenant_id", "incident_id", "source_incident_version",
                         name="uq_agent_run_incident_version"),
        CheckConstraint("model_calls >= 0 AND model_calls <= 4", name="ck_agent_model_budget"),
        CheckConstraint("tool_calls >= 0 AND tool_calls <= 8", name="ck_agent_tool_budget"),
    )
    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_incident_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    model_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    trace: Mapped[dict] = mapped_column(JSONB, nullable=False)


class InvestigationEvidenceRecord(CoreRecord, Base):
    __tablename__ = "investigation_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "report_id"],
            ["investigation_reports.tenant_id", "investigation_reports.id"],
            name="fk_investigation_evidence_report",
        ),
        Index("ix_investigation_evidence_report", "tenant_id", "report_id"),
    )

    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    polarity: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class InvestigationModelReviewRecord(CoreRecord, Base):
    __tablename__ = "investigation_model_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "report_id"],
            ["investigation_reports.tenant_id", "investigation_reports.id"],
            name="fk_investigation_model_reviews_report",
        ),
        UniqueConstraint("tenant_id", "report_id", name="uq_model_review_report"),
    )

    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500))
    cited_evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    uncertainties: Mapped[list] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRunRecord(CoreRecord, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_agent_runs_incident",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_runs_tenant_id"),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    max_llm_calls: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RemediationPlanRecord(CoreRecord, Base):
    __tablename__ = "remediation_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_remediation_plans_incident",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_run_id"],
            ["agent_runs.tenant_id", "agent_runs.id"],
            name="fk_remediation_plans_agent_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_report_id"],
            ["investigation_reports.tenant_id", "investigation_reports.id"],
            name="fk_remediation_plans_report",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_remediation_plans_tenant_id"),
        UniqueConstraint("tenant_id", "source_report_id", name="uq_remediation_plan_report"),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    agent_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    source_report_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_incident_version: Mapped[int | None] = mapped_column(BigInteger)
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    catalog_version: Mapped[str | None] = mapped_column(String(80))
    root_cause_summary: Mapped[str | None] = mapped_column(String(500))
    risk_summary: Mapped[str | None] = mapped_column(String(500))
    blast_radius: Mapped[str | None] = mapped_column(String(500))


class ActionRecord(CoreRecord, Base):
    __tablename__ = "actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_actions_incident",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "plan_id"],
            ["remediation_plans.tenant_id", "remediation_plans.id"],
            name="fk_actions_plan",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_actions_tenant_id"),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ApprovalRecord(CoreRecord, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_approvals_action",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_decision_id"],
            ["policy_decisions.tenant_id", "policy_decisions.id"],
            name="fk_approvals_policy_decision",
        ),
    )

    action_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_decision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    requested_by: Mapped[str | None] = mapped_column(String(120))
    decided_by: Mapped[str | None] = mapped_column(String(120))
    decision_reason: Mapped[str | None] = mapped_column(String(300))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyDecisionRecord(CoreRecord, Base):
    __tablename__ = "policy_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_policy_decisions_action",
        ),
        Index("ix_policy_decisions_action", "tenant_id", "action_id", "created_at"),
        UniqueConstraint("tenant_id", "id", name="uq_policy_decisions_tenant_id"),
    )

    action_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    required_role: Mapped[str | None] = mapped_column(String(80))
    required_approvals: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ResourceLeaseRecord(CoreRecord, Base):
    __tablename__ = "resource_leases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "resource_key", name="uq_resource_lease_key"),
        ForeignKeyConstraint(
            ["tenant_id", "holder_action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_resource_leases_action",
        ),
    )

    resource_key: Mapped[str] = mapped_column(String(200), nullable=False)
    holder_action_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionAttemptRecord(CoreRecord, Base):
    __tablename__ = "action_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "action_id"],
            ["actions.tenant_id", "actions.id"],
            name="fk_action_attempts_action",
        ),
        UniqueConstraint(
            "tenant_id", "action_id", "attempt_number", name="uq_action_attempt_number"
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_action_attempt_idempotency"),
    )

    action_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AlertRecord(CoreRecord, Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_alerts_tenant_id"),
        UniqueConstraint("tenant_id", "source", "external_id", name="uq_alerts_external"),
    )

    source: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(250), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(250), nullable=False)
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(120), nullable=False)
    service_name: Mapped[str] = mapped_column(String(120), nullable=False)
    severity: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RequestFailureEventRecord(CoreRecord, Base):
    __tablename__ = "request_failure_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_request_failure_tenant_id"),
        UniqueConstraint(
            "tenant_id", "service_name", "source_event_id",
            name="uq_request_failure_source_event",
        ),
        Index("ix_request_failure_trace", "tenant_id", "trace_id"),
    )

    source_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    service_name: Mapped[str] = mapped_column(String(120), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_code: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_name: Mapped[str | None] = mapped_column(String(120))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RequestDerivedSignalRecord(CoreRecord, Base):
    __tablename__ = "request_derived_signals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_request_derived_signal_tenant_id"),
        UniqueConstraint(
            "tenant_id", "detector_version", "identity_key", "window_start",
            name="uq_request_derived_signal_window",
        ),
        CheckConstraint("event_count >= 2", name="ck_request_derived_signal_event_count"),
        CheckConstraint(
            "distinct_trace_count >= 2", name="ck_request_derived_signal_trace_count"
        ),
        CheckConstraint(
            "verification_status = 'VERIFIED_SPAN_FACT'",
            name="ck_request_derived_signal_verified",
        ),
        CheckConstraint("window_end > window_start", name="ck_request_derived_signal_window"),
    )

    detector_version: Mapped[str] = mapped_column(String(80), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_name: Mapped[str] = mapped_column(String(120), nullable=False)
    failure_code: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_name: Mapped[str | None] = mapped_column(String(120))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    distinct_trace_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(40), nullable=False)


class RequestDerivedSignalEventRecord(CoreRecord, Base):
    __tablename__ = "request_derived_signal_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "signal_id"],
            ["request_derived_signals.tenant_id", "request_derived_signals.id"],
            name="fk_request_derived_signal_event_signal",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "request_event_id"],
            ["request_failure_events.tenant_id", "request_failure_events.id"],
            name="fk_request_derived_signal_event_request",
        ),
        UniqueConstraint(
            "tenant_id", "signal_id", "request_event_id",
            name="uq_request_derived_signal_event_member",
        ),
        CheckConstraint(
            "verification_status = 'VERIFIED_SPAN_FACT'",
            name="ck_request_derived_signal_event_verified",
        ),
    )

    signal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(40), nullable=False)
    verification_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestSignalWindowRecord(CoreRecord, Base):
    __tablename__ = "request_signal_windows"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_request_signal_window_tenant_id"),
        UniqueConstraint(
            "tenant_id", "detector_version", "window_start",
            name="uq_request_signal_window",
        ),
        CheckConstraint("window_end > window_start", name="ck_request_signal_window_bounds"),
        CheckConstraint("event_count >= 0", name="ck_request_signal_window_events"),
        CheckConstraint("signal_count >= 0", name="ck_request_signal_window_signals"),
    )

    detector_version: Mapped[str] = mapped_column(String(80), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    snapshot_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signal_count: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RequestSignalDetectorRunRecord(CoreRecord, Base):
    __tablename__ = "request_signal_detector_runs"
    __table_args__ = (
        CheckConstraint("snapshot_complete OR status = 'BLOCKED'",
                        name="ck_incomplete_snapshot_blocked"),
        ForeignKeyConstraint(
            ["tenant_id", "source_window_id"],
            ["request_signal_windows.tenant_id", "request_signal_windows.id"],
            name="fk_request_signal_run_source_window",
        ),
        CheckConstraint(
            "(source_window_id IS NULL AND snapshot_event_ids IS NULL "
            "AND detector_version = 'request-failure-window-v1') OR "
            "(source_window_id IS NOT NULL AND snapshot_event_ids IS NOT NULL "
            "AND detector_version LIKE 'late-v1:%')",
            name="ck_request_signal_run_snapshot_kind",
        ),
        UniqueConstraint(
            "tenant_id", "detector_version", "window_start",
            name="uq_request_signal_detector_run",
        ),
        CheckConstraint("window_end > window_start", name="ck_request_signal_run_bounds"),
        CheckConstraint("attempt_count >= 0", name="ck_request_signal_run_attempts"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'RETRYABLE', 'BLOCKED', 'FINALIZED')",
            name="ck_request_signal_run_status",
        ),
    )

    detector_version: Mapped[str] = mapped_column(String(80), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requeue_history: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    source_window_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    snapshot_event_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    snapshot_complete: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                   default=True, server_default="true")


class IncidentRequestEvidenceRecord(CoreRecord, Base):
    __tablename__ = "incident_request_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_request_evidence_incident",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "request_event_id"],
            ["request_failure_events.tenant_id", "request_failure_events.id"],
            name="fk_incident_request_evidence_event",
        ),
        UniqueConstraint(
            "tenant_id", "incident_id", "request_event_id",
            name="uq_incident_request_evidence_pair",
        ),
        CheckConstraint(
            "verification_status = 'VERIFIED_SPAN_FACT'",
            name="ck_incident_request_evidence_verified",
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    incident_version_at_link: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(40), nullable=False)


class IncidentAlertRecord(CoreRecord, Base):
    __tablename__ = "incident_alerts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_alerts_incident",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "alert_id"],
            ["alerts.tenant_id", "alerts.id"],
            name="fk_incident_alerts_alert",
        ),
        UniqueConstraint("tenant_id", "alert_id", name="uq_incident_alerts_alert"),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    alert_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)


class IncidentRelationRecord(CoreRecord, Base):
    __tablename__ = "incident_relations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "from_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_relations_from",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "to_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_relations_to",
        ),
        UniqueConstraint(
            "tenant_id",
            "from_incident_id",
            "to_incident_id",
            "relation_type",
            name="uq_incident_relation",
        ),
        CheckConstraint("from_incident_id <> to_incident_id", name="ck_incident_relation_distinct"),
    )

    from_incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    from_version_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_version_before: Mapped[int | None] = mapped_column(BigInteger)


class IncidentTimelineRecord(CoreRecord, Base):
    __tablename__ = "incident_timeline"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_incident_timeline_incident",
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)


class OutboxEventRecord(CoreRecord, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at"),)

    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboxMessageRecord(CoreRecord, Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "consumer", "message_id", name="uq_inbox_message"),
    )

    consumer: Mapped[str] = mapped_column(String(80), nullable=False)
    message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)


class ServiceRecord(CoreRecord, Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_services_tenant_id"),
        UniqueConstraint("tenant_id", "environment", "slug", name="uq_services_slug"),
    )

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    environment: Mapped[str] = mapped_column(String(80), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    criticality: Mapped[str] = mapped_column(String(20), nullable=False)


class ResourceRecord(CoreRecord, Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_resources_tenant_id"),
        UniqueConstraint("tenant_id", "environment", "slug", name="uq_resources_slug"),
    )

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    environment: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False)


class ServiceDependencyRecord(CoreRecord, Base):
    __tablename__ = "service_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_service_dependencies_service",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "depends_on_service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_service_dependencies_target_service",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "depends_on_resource_id"],
            ["resources.tenant_id", "resources.id"],
            name="fk_service_dependencies_target_resource",
        ),
        CheckConstraint(
            "(depends_on_service_id IS NULL) <> (depends_on_resource_id IS NULL)",
            name="ck_service_dependency_one_target",
        ),
    )

    service_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    depends_on_service_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    depends_on_resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class ChangeEventRecord(CoreRecord, Base):
    __tablename__ = "change_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_change_events_service",
        ),
        UniqueConstraint("tenant_id", "source", "external_id", name="uq_change_events_source"),
        Index("ix_change_events_service_time", "tenant_id", "service_id", "occurred_at"),
    )

    service_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(250), nullable=False)
    change_type: Mapped[str] = mapped_column(String(80), nullable=False)
    from_ref: Mapped[str | None] = mapped_column(String(250))
    to_ref: Mapped[str | None] = mapped_column(String(250))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False)


class CorrelationDecisionRecord(CoreRecord, Base):
    __tablename__ = "correlation_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "alert_id"],
            ["alerts.tenant_id", "alerts.id"],
            name="fk_correlation_decisions_alert",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_correlation_decisions_candidate",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "result_incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            name="fk_correlation_decisions_result",
        ),
        UniqueConstraint("tenant_id", "alert_id", name="uq_correlation_decision_alert"),
    )

    alert_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    candidate_incident_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    result_incident_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    total_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    matched_features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    conflicting_features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(80), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
