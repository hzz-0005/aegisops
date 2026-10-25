"""Manual Incident corrections that preserve decision and timeline history."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aegisops.application.incident_lifecycle import (
    IncidentNotFound,
    IncidentVersionConflict,
    incident_from_record,
)
from aegisops.config import Settings
from aegisops.domain.models import Incident, IncidentStatus, ProcessingPhase
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionRecord,
    AlertRecord,
    IncidentAlertRecord,
    IncidentRecord,
    IncidentRelationRecord,
    IncidentTimelineRecord,
    OutboxEventRecord,
)


class IncidentCorrectionRejected(Exception):
    pass


def _lock_tenant(session: Session, tenant_id: UUID) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(tenant_id)},
    )


def _locked_incidents(
    session: Session, tenant_id: UUID, ids: tuple[UUID, ...]
) -> dict[UUID, IncidentRecord]:
    rows = session.scalars(
        select(IncidentRecord)
        .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id.in_(ids))
        .order_by(IncidentRecord.id)
        .with_for_update()
    ).all()
    if len(rows) != len(ids):
        raise IncidentNotFound
    return {row.id: row for row in rows}


def _reject_if_actions(session: Session, tenant_id: UUID, ids: tuple[UUID, ...]) -> None:
    action_id = session.scalar(
        select(ActionRecord.id)
        .where(ActionRecord.tenant_id == tenant_id, ActionRecord.incident_id.in_(ids))
        .limit(1)
    )
    if action_id is not None:
        raise IncidentCorrectionRejected(
            "incident with recorded actions cannot move alerts through a simple merge or split"
        )


def _timeline(
    tenant_id: UUID, incident_id: UUID, event_type: str, summary: str, details: dict
) -> IncidentTimelineRecord:
    return IncidentTimelineRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        incident_id=incident_id,
        event_type=event_type,
        summary=summary,
        details=details,
    )


def merge_incidents(
    settings: Settings,
    source_id: UUID,
    target_id: UUID,
    *,
    source_version: int,
    target_version: int,
    reason: str,
    operator_id: str,
) -> tuple[UUID, int]:
    if source_id == target_id:
        raise IncidentCorrectionRejected("source and target must differ")
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        _lock_tenant(session, tenant_id)
        records = _locked_incidents(session, tenant_id, (source_id, target_id))
        source, target = records[source_id], records[target_id]
        if source.version != source_version or target.version != target_version:
            raise IncidentVersionConflict
        if source.status != IncidentStatus.OPEN or target.status != IncidentStatus.OPEN:
            raise IncidentCorrectionRejected("both incidents must be OPEN for simple merge")
        _reject_if_actions(session, tenant_id, (source_id, target_id))
        links = session.scalars(
            select(IncidentAlertRecord)
            .where(
                IncidentAlertRecord.tenant_id == tenant_id,
                IncidentAlertRecord.incident_id == source_id,
            )
            .order_by(IncidentAlertRecord.id)
        ).all()
        if not links:
            raise IncidentCorrectionRejected("source incident has no alerts")
        now = datetime.now(UTC)
        alert_ids = [str(link.alert_id) for link in links]
        for link in links:
            link.incident_id = target_id
            link.updated_at = now
            link.version += 1
        superseded = incident_from_record(source).transition_status(IncidentStatus.SUPERSEDED)
        source.status = superseded.status.value
        source.processing_phase = superseded.processing_phase.value
        source.updated_at = superseded.updated_at
        source.version = superseded.version
        target.opened_at = min(target.opened_at, source.opened_at)
        target.severity = min(target.severity, source.severity)
        target.updated_at = now
        target.version += 1
        session.add(
            IncidentRelationRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                from_incident_id=source_id,
                to_incident_id=target_id,
                relation_type="MERGED_INTO",
                operator_id=operator_id,
                reason=reason,
                from_version_before=source_version,
                to_version_before=target_version,
            )
        )
        audit = {
            "source_incident_id": str(source_id),
            "target_incident_id": str(target_id),
            "alert_ids": alert_ids,
            "operator_id": operator_id,
            "reason": reason,
            "source_version_before": source_version,
            "source_version_after": source.version,
            "target_version_before": target_version,
            "target_version_after": target.version,
        }
        session.add(
            _timeline(tenant_id, source_id, "MERGED_OUT", "Incident merged into another", audit)
        )
        session.add(
            _timeline(tenant_id, target_id, "MERGED_IN", "Incident absorbed another", audit)
        )
        session.add(OutboxEventRecord(
            id=uuid4(), tenant_id=tenant_id, event_type="incident.updated.v1",
            payload={"incident_id": str(target_id), "incident_version": target.version},
        ))
    return target_id, len(links)


def split_incident(
    settings: Settings,
    source_id: UUID,
    *,
    expected_version: int,
    alert_ids: list[UUID],
    title: str,
    reason: str,
    operator_id: str,
) -> tuple[UUID, int]:
    selected = set(alert_ids)
    if len(selected) != len(alert_ids):
        raise IncidentCorrectionRejected("alert IDs must be unique")
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        _lock_tenant(session, tenant_id)
        source = _locked_incidents(session, tenant_id, (source_id,))[source_id]
        if source.version != expected_version:
            raise IncidentVersionConflict
        if source.status != IncidentStatus.OPEN:
            raise IncidentCorrectionRejected("source incident must be OPEN for simple split")
        _reject_if_actions(session, tenant_id, (source_id,))
        links = session.scalars(
            select(IncidentAlertRecord)
            .where(
                IncidentAlertRecord.tenant_id == tenant_id,
                IncidentAlertRecord.incident_id == source_id,
            )
            .order_by(IncidentAlertRecord.id)
        ).all()
        current = {link.alert_id for link in links}
        if not selected or not selected < current:
            raise IncidentCorrectionRejected(
                "split must select a nonempty proper subset of the incident's alerts"
            )
        moved = session.scalars(
            select(AlertRecord).where(
                AlertRecord.tenant_id == tenant_id, AlertRecord.id.in_(selected)
            )
        ).all()
        if len(moved) != len(selected):
            raise IncidentCorrectionRejected("selected alert is missing")
        opened_at = min(alert.occurred_at for alert in moved)
        severity = min(alert.severity for alert in moved)
        domain = Incident(
            tenant_id=tenant_id,
            title=title,
            severity=severity,
            opened_at=opened_at,
        ).transition_phase(ProcessingPhase.READY_FOR_INVESTIGATION)
        new_incident = IncidentRecord(
            id=domain.id,
            tenant_id=tenant_id,
            title=domain.title,
            status=domain.status.value,
            processing_phase=domain.processing_phase.value,
            severity=domain.severity,
            opened_at=domain.opened_at,
            version=domain.version,
        )
        new_incident_id = new_incident.id
        session.add(new_incident)
        session.flush()
        session.add(
            OutboxEventRecord(
                id=uuid4(), tenant_id=tenant_id,
                event_type="incident.created.v1",
                payload={"incident_id": str(new_incident.id)},
            )
        )
        now = datetime.now(UTC)
        for link in links:
            if link.alert_id in selected:
                link.incident_id = new_incident.id
                link.updated_at = now
                link.version += 1
        source.updated_at = now
        source.version += 1
        session.add(OutboxEventRecord(
            id=uuid4(), tenant_id=tenant_id, event_type="incident.updated.v1",
            payload={"incident_id": str(source_id), "incident_version": source.version},
        ))
        session.add(
            IncidentRelationRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                from_incident_id=new_incident.id,
                to_incident_id=source_id,
                relation_type="SPLIT_FROM",
                operator_id=operator_id,
                reason=reason,
                from_version_before=0,
                to_version_before=expected_version,
            )
        )
        audit = {
            "source_incident_id": str(source_id),
            "new_incident_id": str(new_incident.id),
            "alert_ids": sorted(str(alert_id) for alert_id in selected),
            "operator_id": operator_id,
            "reason": reason,
            "source_version_before": expected_version,
            "source_version_after": source.version,
            "new_incident_version": new_incident.version,
        }
        session.add(
            _timeline(tenant_id, source_id, "SPLIT_OUT", "Alerts moved to a new Incident", audit)
        )
        session.add(
            _timeline(
                tenant_id, new_incident.id, "SPLIT_IN", "Incident created from split alerts", audit
            )
        )
    return new_incident_id, len(selected)
