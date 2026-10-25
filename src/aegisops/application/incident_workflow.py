"""Durable workflow start ledger and idempotent Incident database effects."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.config import Settings
from aegisops.domain.models import IncidentStatus, ProcessingPhase
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentRecord,
    IncidentTimelineRecord,
    IncidentWorkflowStartRecord,
)

from .incident_lifecycle import (
    IncidentNotFound,
    IncidentVersionConflict,
    change_incident_status,
    escalate_incident_phase,
    incident_from_record,
)
from .remediation.resolution import resolve_verified_lab_incident


def workflow_id_for(tenant_id: UUID, incident_id: UUID) -> str:
    return f"incident/{tenant_id}/{incident_id}"


def reserve_workflow_start(settings: Settings, tenant_id: UUID, incident_id: UUID) -> str:
    """Reserve before RPC; stale uncertain starts require reconciliation, never blind restart."""
    now = datetime.now(UTC)
    with session_for(settings.database_url) as session, session.begin():
        incident = session.scalar(
            select(IncidentRecord.id).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
        if incident is None:
            return "SKIPPED_MISSING"
        session.execute(
            insert(IncidentWorkflowStartRecord)
            .values(
                tenant_id=tenant_id,
                incident_id=incident_id,
                workflow_id=workflow_id_for(tenant_id, incident_id),
                state="PENDING",
            )
            .on_conflict_do_nothing(constraint="uq_incident_workflow_start")
        )
        row = session.scalar(
            select(IncidentWorkflowStartRecord)
            .where(
                IncidentWorkflowStartRecord.tenant_id == tenant_id,
                IncidentWorkflowStartRecord.incident_id == incident_id,
            )
            .with_for_update()
        )
        assert row is not None
        if row.state != "PENDING":
            return row.state
        if row.first_attempted_at is not None:
            age = now - row.first_attempted_at
            if age >= timedelta(hours=settings.temporal_start_reconcile_after_hours):
                row.state = "RECONCILIATION_REQUIRED"
                row.version += 1
                row.updated_at = now
                return row.state
        else:
            row.first_attempted_at = now
            row.updated_at = now
            row.version += 1
        return "START"


def confirm_workflow_start(
    settings: Settings, tenant_id: UUID, incident_id: UUID, run_id: str | None
) -> None:
    with session_for(settings.database_url) as session, session.begin():
        row = session.scalar(
            select(IncidentWorkflowStartRecord)
            .where(
                IncidentWorkflowStartRecord.tenant_id == tenant_id,
                IncidentWorkflowStartRecord.incident_id == incident_id,
            )
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("workflow start reservation is missing")
        if row.state == "STARTED":
            return
        if row.state != "PENDING":
            raise RuntimeError(f"cannot confirm workflow start in {row.state}")
        now = datetime.now(UTC)
        row.state = "STARTED"
        row.run_id = run_id
        row.started_at = now
        row.updated_at = now
        row.version += 1


def reconcile_existing_workflow_start(
    settings: Settings,
    tenant_id: UUID,
    incident_id: UUID,
    *,
    expected_start_version: int,
    run_id: str,
    started_at: datetime,
    reason: str,
    operator_id: str,
) -> str:
    """Record a Temporal workflow found by its stable ID after an uncertain start."""
    with session_for(settings.database_url) as session, session.begin():
        row = session.scalar(
            select(IncidentWorkflowStartRecord)
            .where(
                IncidentWorkflowStartRecord.tenant_id == tenant_id,
                IncidentWorkflowStartRecord.incident_id == incident_id,
            )
            .with_for_update()
        )
        if row is None:
            return "MISSING"
        if row.version != expected_start_version:
            return "VERSION_CONFLICT"
        if row.state != "RECONCILIATION_REQUIRED":
            return "INVALID_STATE"
        now = datetime.now(UTC)
        previous_version = row.version
        row.state = "STARTED"
        row.run_id = run_id
        row.started_at = started_at
        row.updated_at = now
        row.version += 1
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="WORKFLOW_START_RECONCILED",
                summary="Operator confirmed Temporal workflow exists",
                details={
                    "workflow_id": row.workflow_id,
                    "run_id": run_id,
                    "operator_id": operator_id,
                    "reason": reason,
                    "old_version": previous_version,
                    "new_version": row.version,
                },
            )
        )
        return "STARTED"


def mark_investigating(settings: Settings, tenant_id: UUID, incident_id: UUID) -> str:
    """Lock the Incident so activity retries cannot duplicate the transition."""
    with session_for(settings.database_url) as session, session.begin():
        record = session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id)
            .with_for_update()
        )
        if record is None:
            return "SKIPPED_MISSING"
        if record.status not in {IncidentStatus.OPEN, IncidentStatus.MITIGATING}:
            return "SKIPPED_TERMINAL"
        if record.processing_phase != ProcessingPhase.READY_FOR_INVESTIGATION:
            return "ALREADY_ADVANCED"
        before = incident_from_record(record)
        after = before.transition_phase(ProcessingPhase.INVESTIGATING)
        record.processing_phase = after.processing_phase.value
        record.updated_at = after.updated_at
        record.version = after.version
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="WORKFLOW_STARTED",
                summary="Incident workflow entered investigation",
                details={
                    "workflow_id": workflow_id_for(tenant_id, incident_id),
                    "from": before.processing_phase.value,
                    "to": after.processing_phase.value,
                    "old_version": before.version,
                    "new_version": after.version,
                },
            )
        )
        return "INVESTIGATING"


def apply_operator_decision(
    settings: Settings,
    tenant_id: UUID,
    incident_id: UUID,
    decision: str,
    expected_version: int,
    reason: str,
    operator_id: str,
) -> str:
    try:
        if decision == "RESOLVE":
            return resolve_verified_lab_incident(
                settings,
                tenant_id,
                incident_id,
                expected_version=expected_version,
                reason=reason,
                operator_id=operator_id,
            )
        if decision == "ESCALATE":
            escalate_incident_phase(
                settings,
                incident_id,
                expected_version=expected_version,
                reason=reason,
                operator_id=operator_id,
                tenant_id=tenant_id,
            )
            return "ESCALATED"
        if decision == "CANCEL":
            change_incident_status(
                settings,
                incident_id,
                expected_version=expected_version,
                target=IncidentStatus.CANCELLED,
                reason=reason,
                operator_id=operator_id,
                tenant_id=tenant_id,
            )
            return "CANCELLED"
    except IncidentVersionConflict:
        return "VERSION_CONFLICT"
    except IncidentNotFound:
        return "SKIPPED_MISSING"
    except ValueError:
        return "INVALID_TRANSITION"
    return "INVALID_DECISION"


def expire_operator_wait(
    settings: Settings, tenant_id: UUID, incident_id: UUID, review_timeout_seconds: int
) -> str:
    """Escalate an unreviewed Incident once; retries cannot duplicate the timeline."""
    with session_for(settings.database_url) as session, session.begin():
        record = session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id)
            .with_for_update()
        )
        if record is None:
            return "SKIPPED_MISSING"
        if record.status not in {IncidentStatus.OPEN, IncidentStatus.MITIGATING}:
            return "SKIPPED_TERMINAL"
        if record.processing_phase != ProcessingPhase.INVESTIGATING:
            return "ALREADY_ADVANCED"
        before = incident_from_record(record)
        after = before.transition_phase(ProcessingPhase.ESCALATED)
        record.processing_phase = after.processing_phase.value
        record.updated_at = after.updated_at
        record.version = after.version
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="PHASE_CHANGED",
                summary="INVESTIGATING → ESCALATED",
                details={
                    "from": before.processing_phase.value,
                    "to": after.processing_phase.value,
                    "reason": f"operator review timed out after {review_timeout_seconds} seconds",
                    "operator_id": "workflow-timeout",
                    "old_version": before.version,
                    "new_version": after.version,
                },
            )
        )
        return "ESCALATED"
