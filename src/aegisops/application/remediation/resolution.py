"""Operator resolution after fresh Lab recovery evidence, with idempotent DB effects."""

from uuid import UUID

from sqlalchemy import select

from aegisops.application.incident_lifecycle import incident_from_record
from aegisops.config import Settings
from aegisops.domain.models import IncidentStatus
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionAttemptRecord,
    ActionRecord,
    AlertRecord,
    IncidentAlertRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    RemediationPlanRecord,
)

from .recovery import assess_lab_recovery


def _already_resolved(
    session,
    tenant_id: UUID,
    incident_id: UUID,
    expected_version: int,
    reason: str,
    operator_id: str,
) -> bool:
    events = session.scalars(
        select(IncidentTimelineRecord)
        .where(
            IncidentTimelineRecord.tenant_id == tenant_id,
            IncidentTimelineRecord.incident_id == incident_id,
            IncidentTimelineRecord.event_type == "RECOVERY_RESOLVED",
        )
        .order_by(IncidentTimelineRecord.created_at.desc())
    ).all()
    return any(
        event.details.get("old_version") == expected_version
        and event.details.get("operator_id") == operator_id
        and event.details.get("reason") == reason
        for event in events
    )


def resolve_verified_lab_incident(
    settings: Settings,
    tenant_id: UUID,
    incident_id: UUID,
    *,
    expected_version: int,
    reason: str,
    operator_id: str,
) -> str:
    if len(reason.strip()) < 3 or not operator_id.strip():
        return "INVALID_DECISION"
    with session_for(settings.database_url) as session:
        current = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
        if current is None:
            return "SKIPPED_MISSING"
        if current.status == IncidentStatus.RESOLVED:
            return (
                "RESOLVED"
                if _already_resolved(
                    session, tenant_id, incident_id, expected_version, reason, operator_id
                )
                else "VERSION_CONFLICT"
            )
        if current.version != expected_version:
            return "VERSION_CONFLICT"
    assessment = assess_lab_recovery(settings, tenant_id, incident_id)
    if assessment is None or not assessment.recovery_ready:
        return "NOT_READY"
    with session_for(settings.database_url) as session, session.begin():
        record = session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id)
            .with_for_update()
        )
        if record is None:
            return "SKIPPED_MISSING"
        if record.status == IncidentStatus.RESOLVED:
            return (
                "RESOLVED"
                if _already_resolved(
                    session, tenant_id, incident_id, expected_version, reason, operator_id
                )
                else "VERSION_CONFLICT"
            )
        if record.version != expected_version or record.version != assessment.incident_version:
            return "VERSION_CONFLICT"
        action = session.scalar(
            select(ActionRecord)
            .where(
                ActionRecord.tenant_id == tenant_id,
                ActionRecord.incident_id == incident_id,
                ActionRecord.id == assessment.action_id,
            )
            .with_for_update()
        )
        if action is None or action.status not in {"SUCCEEDED", "RECOVERY_OBSERVED"}:
            return "NOT_READY"
        plan = session.scalar(
            select(RemediationPlanRecord).where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.id == action.plan_id,
            )
        )
        attempt = session.scalar(
            select(ActionAttemptRecord).where(
                ActionAttemptRecord.tenant_id == tenant_id,
                ActionAttemptRecord.action_id == action.id,
                ActionAttemptRecord.id == assessment.last_attempt_id,
            )
        )
        if plan is None or plan.source_incident_version != record.version or attempt is None:
            return "NOT_READY"
        verification = attempt.result.get("verification", {})
        action_type = action.snapshot.get("action_type")
        if (
            verification.get("payment_ready") is not True
            or not (
                (action_type == "restart_cache" and verification.get("redis_ping") is True)
                or (action_type == "restart_service" and verification.get("order_ready") is True)
                or (
                    action_type == "rollback_service"
                    and verification.get("order_ready") is True
                    and verification.get("baseline_deployed") is True
                    and verification.get("target_fingerprint_matches") is True
                )
            )
        ):
            return "NOT_READY"
        alerts = session.scalars(
            select(AlertRecord)
            .join(
                IncidentAlertRecord,
                (IncidentAlertRecord.tenant_id == AlertRecord.tenant_id)
                & (IncidentAlertRecord.alert_id == AlertRecord.id),
            )
            .where(
                IncidentAlertRecord.tenant_id == tenant_id,
                IncidentAlertRecord.incident_id == incident_id,
            )
            .with_for_update(of=AlertRecord)
        ).all()
        if not alerts or any(alert.status != "RESOLVED" for alert in alerts):
            return "NOT_READY"
        before = incident_from_record(record)
        try:
            verifying = before.begin_verified_resolution()
            resolved = verifying.transition_status(
                IncidentStatus.RESOLVED, verification_passed=True
            )
        except ValueError:
            return "INVALID_STATE"
        record.status = resolved.status.value
        record.processing_phase = resolved.processing_phase.value
        record.resolved_at = resolved.resolved_at
        record.version = resolved.version
        record.updated_at = resolved.updated_at
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="RECOVERY_VERIFIED",
                summary="Action and telemetry evidence verified before resolution",
                details={
                    "action_id": str(action.id),
                    "attempt_id": str(attempt.id),
                    "alert_ids": [str(alert.id) for alert in alerts],
                    "payment_checked_at": assessment.checked_at.isoformat(),
                    "old_version": before.version,
                    "new_version": verifying.version,
                },
            )
        )
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="RECOVERY_RESOLVED",
                summary="Incident resolved after operator review of recovery evidence",
                details={
                    "operator_id": operator_id.strip(),
                    "reason": reason.strip(),
                    "action_id": str(action.id),
                    "attempt_id": str(attempt.id),
                    "old_version": before.version,
                    "new_version": resolved.version,
                    "resolved_at": (
                        resolved.resolved_at.isoformat() if resolved.resolved_at else None
                    ),
                },
            )
        )
        return "RESOLVED"
