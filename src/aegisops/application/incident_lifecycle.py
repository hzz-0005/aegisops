"""Operator initiated Incident state changes with an auditable timeline."""

from uuid import UUID, uuid4

from sqlalchemy import select

from aegisops.config import Settings
from aegisops.domain.models import Incident, IncidentStatus, ProcessingPhase
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import IncidentRecord, IncidentTimelineRecord


class IncidentNotFound(Exception):
    pass


class IncidentVersionConflict(Exception):
    pass


def escalate_incident_phase(
    settings: Settings,
    incident_id: UUID,
    *,
    expected_version: int,
    reason: str,
    operator_id: str,
    tenant_id: UUID | None = None,
) -> Incident:
    scoped_tenant_id = tenant_id or settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        record = session.scalar(
            select(IncidentRecord)
            .where(
                IncidentRecord.tenant_id == scoped_tenant_id,
                IncidentRecord.id == incident_id,
            )
            .with_for_update()
        )
        if record is None:
            raise IncidentNotFound
        if record.version != expected_version:
            raise IncidentVersionConflict
        before = incident_from_record(record)
        after = before.transition_phase(ProcessingPhase.ESCALATED)
        record.processing_phase = after.processing_phase.value
        record.updated_at = after.updated_at
        record.version = after.version
        session.add(
            IncidentTimelineRecord(
                id=uuid4(),
                tenant_id=record.tenant_id,
                incident_id=record.id,
                event_type="PHASE_CHANGED",
                summary=f"{before.processing_phase.value} → ESCALATED",
                details={
                    "from": before.processing_phase.value,
                    "to": "ESCALATED",
                    "reason": reason,
                    "operator_id": operator_id,
                    "old_version": expected_version,
                    "new_version": after.version,
                },
            )
        )
    return after


def incident_from_record(record: IncidentRecord) -> Incident:
    return Incident(
        id=record.id,
        tenant_id=record.tenant_id,
        title=record.title,
        status=IncidentStatus(record.status),
        processing_phase=ProcessingPhase(record.processing_phase),
        severity=record.severity,
        correlation_key=record.correlation_key,
        primary_service_id=record.primary_service_id,
        opened_at=record.opened_at,
        resolved_at=record.resolved_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        version=record.version,
    )


def change_incident_status(
    settings: Settings,
    incident_id: UUID,
    *,
    expected_version: int,
    target: IncidentStatus,
    reason: str,
    operator_id: str,
    tenant_id: UUID | None = None,
) -> Incident:
    """Lock the row, apply domain rules, and write state and timeline atomically."""
    scoped_tenant_id = tenant_id or settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        record = session.scalar(
            select(IncidentRecord)
            .where(
                IncidentRecord.tenant_id == scoped_tenant_id,
                IncidentRecord.id == incident_id,
            )
            .with_for_update()
        )
        if record is None:
            raise IncidentNotFound
        if record.version != expected_version:
            raise IncidentVersionConflict
        before = incident_from_record(record)
        after = before.transition_status(target)
        record.status = after.status.value
        record.processing_phase = after.processing_phase.value
        record.resolved_at = after.resolved_at
        record.updated_at = after.updated_at
        record.version = after.version
        session.add(
            IncidentTimelineRecord(
                id=uuid4(),
                tenant_id=record.tenant_id,
                incident_id=record.id,
                event_type="STATUS_CHANGED",
                summary=f"{before.status.value} → {after.status.value}",
                details={
                    "from": before.status.value,
                    "to": after.status.value,
                    "reason": reason,
                    "operator_id": operator_id,
                    "old_version": expected_version,
                    "new_version": after.version,
                    "phase_from": before.processing_phase.value,
                    "phase_to": after.processing_phase.value,
                },
            )
        )
    return after
