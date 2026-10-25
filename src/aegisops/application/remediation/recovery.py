"""Read-only recovery assessment; execution success alone does not resolve an Incident."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from aegisops.config import Profile, Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionAttemptRecord,
    ActionRecord,
    AlertRecord,
    IncidentAlertRecord,
    IncidentRecord,
    RemediationPlanRecord,
)


class RecoveryAlertView(BaseModel):
    id: UUID
    name: str
    status: str
    ended_at: datetime | None


class RecoveryAssessmentView(BaseModel):
    incident_id: UUID
    incident_version: int
    checked_at: datetime
    action_id: UUID | None
    action_status: str | None
    action_type: str | None
    action_source_incident_version: int | None
    last_attempt_id: UUID | None
    execution_readiness_recorded: bool
    payment_ready_now: bool
    order_ready_now: bool | None
    deployment_good_now: bool | None
    alerts: list[RecoveryAlertView]
    recovery_ready: bool
    limitations: list[str]


def assess_lab_recovery(
    settings: Settings, tenant_id: UUID, incident_id: UUID
) -> RecoveryAssessmentView | None:
    if settings.profile != Profile.DEV:
        raise ValueError("Lab recovery assessment requires the dev profile")
    with session_for(settings.database_url) as session:
        incident = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
        if incident is None:
            return None
        alert_rows = session.scalars(
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
            .order_by(AlertRecord.occurred_at, AlertRecord.id)
        ).all()
        alerts = [
            RecoveryAlertView(
                id=row.id,
                name=row.payload.get("alertname", "unknown"),
                status=row.status,
                ended_at=row.ends_at,
            )
            for row in alert_rows
        ]
        action = session.scalar(
            select(ActionRecord)
            .join(
                RemediationPlanRecord,
                (RemediationPlanRecord.tenant_id == ActionRecord.tenant_id)
                & (RemediationPlanRecord.id == ActionRecord.plan_id),
            )
            .where(
                ActionRecord.tenant_id == tenant_id,
                ActionRecord.incident_id == incident_id,
            )
            .order_by(RemediationPlanRecord.created_at.desc(), ActionRecord.created_at.desc())
            .limit(1)
        )
        attempt = (
            session.scalar(
                select(ActionAttemptRecord)
                .where(
                    ActionAttemptRecord.tenant_id == tenant_id,
                    ActionAttemptRecord.action_id == action.id,
                )
                .order_by(ActionAttemptRecord.attempt_number.desc())
                .limit(1)
            )
            if action is not None
            else None
        )
        incident_version = incident.version
        incident_status = incident.status
        action_id = action.id if action else None
        action_status = action.status if action else None
        action_type = action.snapshot.get("action_type") if action else None
        source_version = (
            session.scalar(
                select(RemediationPlanRecord.source_incident_version).where(
                    RemediationPlanRecord.tenant_id == tenant_id,
                    RemediationPlanRecord.id == action.plan_id,
                )
            )
            if action is not None
            else None
        )
        attempt_id = attempt.id if attempt else None
        verification = attempt.result.get("verification", {}) if attempt else {}
        execution_readiness_recorded = verification.get("payment_ready") is True and (
            (action_type == "restart_cache" and verification.get("redis_ping") is True)
            or (action_type == "restart_service" and verification.get("order_ready") is True)
            or (
                action_type == "rollback_service"
                and verification.get("order_ready") is True
                and verification.get("baseline_deployed") is True
                and verification.get("target_fingerprint_matches") is True
            )
        )
    payment_ready = False
    try:
        response = httpx.get(f"{settings.lab_payment_url.rstrip('/')}/health/ready", timeout=3)
        payment_ready = response.status_code == 200 and response.json().get("status") == "ready"
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    order_ready: bool | None = None
    deployment_good: bool | None = None
    if action_type in {"restart_service", "rollback_service"}:
        order_ready = False
        try:
            response = httpx.get(f"{settings.lab_order_url.rstrip('/')}/health/ready", timeout=3)
            order_ready = response.status_code == 200 and response.json().get("status") == "ready"
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    if action_type == "rollback_service":
        deployment_good = False
        try:
            response = httpx.get(f"{settings.lab_order_url.rstrip('/')}/deployment", timeout=3)
            deployment_good = response.status_code == 200 and response.json() == {
                "change_id": "baseline",
                "bad_deployment": False,
            }
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    limitations: list[str] = []
    if not alerts:
        limitations.append("incident has no linked alerts")
    if any(item.status != "RESOLVED" for item in alerts):
        limitations.append("one or more linked alerts are still firing")
    if action_status not in {"SUCCEEDED", "RECOVERY_OBSERVED"}:
        limitations.append("no completed Lab recovery action")
    if source_version != incident_version:
        limitations.append("action was based on a different Incident version")
    if not execution_readiness_recorded:
        limitations.append("no successful execution readiness evidence")
    if not payment_ready:
        limitations.append("Payment is not ready at assessment time")
    if order_ready is False:
        limitations.append("Order is not ready at assessment time")
    if deployment_good is False:
        limitations.append("Order baseline deployment is not active at assessment time")
    if incident_status != "OPEN":
        limitations.append(f"incident status is {incident_status}")
    return RecoveryAssessmentView(
        incident_id=incident_id,
        incident_version=incident_version,
        checked_at=datetime.now(UTC),
        action_id=action_id,
        action_status=action_status,
        action_type=action_type,
        action_source_incident_version=source_version,
        last_attempt_id=attempt_id,
        execution_readiness_recorded=execution_readiness_recorded,
        payment_ready_now=payment_ready,
        order_ready_now=order_ready,
        deployment_good_now=deployment_good,
        alerts=alerts,
        recovery_ready=not limitations,
        limitations=limitations,
    )
