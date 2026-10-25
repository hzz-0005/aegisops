from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from aegisops.application.contracts import AlertReceivedEvent
from aegisops.application.correlation_rules import (
    RULE_VERSION,
    EvidenceScore,
    evaluate_pair,
    has_ambiguous_candidates,
)
from aegisops.application.normalization import normalized_from_record
from aegisops.config import Settings
from aegisops.domain.models import Incident, ProcessingPhase
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    CorrelationDecisionRecord,
    InboxMessageRecord,
    IncidentAlertRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    OutboxEventRecord,
    ServiceDependencyRecord,
    ServiceRecord,
)

ACTIVE_STATUSES = ("OPEN", "MITIGATING")


def services_related(session: Session, tenant_id: UUID, left: str, right: str) -> bool:
    if left == right:
        return True
    rows = session.scalars(
        select(ServiceRecord).where(
            ServiceRecord.tenant_id == tenant_id,
            ServiceRecord.environment == "lab",
            ServiceRecord.slug.in_((left, right)),
        )
    ).all()
    ids = {row.slug: row.id for row in rows}
    if left not in ids or right not in ids:
        return False
    dependencies = session.scalars(
        select(ServiceDependencyRecord).where(
            ServiceDependencyRecord.tenant_id == tenant_id,
            ServiceDependencyRecord.service_id.in_((ids[left], ids[right])),
        )
    ).all()
    targets: dict[str, set[str]] = {left: set(), right: set()}
    for dependency in dependencies:
        owner = left if dependency.service_id == ids[left] else right
        if dependency.depends_on_service_id:
            targets[owner].add(f"service:{dependency.depends_on_service_id}")
        if dependency.depends_on_resource_id:
            targets[owner].add(f"resource:{dependency.depends_on_resource_id}")
    return bool(
        targets[left] & targets[right]
        or f"service:{ids[right]}" in targets[left]
        or f"service:{ids[left]}" in targets[right]
    )


def correlate_alert(settings: Settings, event: AlertReceivedEvent) -> bool:
    """Return False for a redelivered message; commit inbox and effects together."""
    with session_for(settings.database_url) as session, session.begin():
        duplicate = session.scalar(
            select(InboxMessageRecord.id).where(
                InboxMessageRecord.tenant_id == event.tenant_id,
                InboxMessageRecord.consumer == "incident-correlation-v1",
                InboxMessageRecord.message_id == event.message_id,
            )
        )
        if duplicate is not None:
            return False
        alert = session.scalar(
            select(AlertRecord).where(
                AlertRecord.tenant_id == event.tenant_id,
                AlertRecord.id == event.alert_id,
            )
        )
        if alert is None:
            raise ValueError("alert referenced by event is missing")
        normalized = normalized_from_record(alert)
        key = normalized.correlation_key()
        # Candidate search spans identities; serialize tenant decisions across workers.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(event.tenant_id)},
        )
        duplicate_after_lock = session.scalar(
            select(InboxMessageRecord.id).where(
                InboxMessageRecord.tenant_id == event.tenant_id,
                InboxMessageRecord.consumer == "incident-correlation-v1",
                InboxMessageRecord.message_id == event.message_id,
            )
        )
        if duplicate_after_lock is not None:
            return False
        now = datetime.now(UTC)
        occurred_at = normalized.occurred_at
        rows = session.execute(
            select(IncidentRecord, AlertRecord)
            .join(
                IncidentAlertRecord,
                and_(
                    IncidentAlertRecord.tenant_id == IncidentRecord.tenant_id,
                    IncidentAlertRecord.incident_id == IncidentRecord.id,
                ),
            )
            .join(
                AlertRecord,
                and_(
                    AlertRecord.tenant_id == IncidentAlertRecord.tenant_id,
                    AlertRecord.id == IncidentAlertRecord.alert_id,
                ),
            )
            .where(
                IncidentRecord.tenant_id == event.tenant_id,
                IncidentRecord.status.in_(ACTIVE_STATUSES),
                AlertRecord.status == "FIRING",
                AlertRecord.occurred_at >= occurred_at - timedelta(minutes=15),
                AlertRecord.occurred_at <= occurred_at + timedelta(minutes=15),
            )
            .order_by(IncidentRecord.opened_at, IncidentRecord.id)
            .limit(500)
        ).all()
        incident: IncidentRecord | None = None
        candidate: IncidentRecord | None = None
        best = EvidenceScore(0.0, {}, {})
        candidate_evidence: dict[
            UUID, tuple[IncidentRecord, dict[str, float], dict[str, float]]
        ] = {}
        for existing_incident, existing_alert in rows:
            previous = normalized_from_record(existing_alert)
            score = evaluate_pair(
                normalized,
                previous,
                shared_dependency=services_related(
                    session, event.tenant_id, normalized.service_id, previous.service_id
                ),
            )
            prior = candidate_evidence.get(existing_incident.id)
            if prior is None:
                candidate_evidence[existing_incident.id] = (
                    existing_incident,
                    score.matched_features,
                    score.conflicting_features,
                )
            else:
                _, matched, conflicting = prior
                if sum(score.matched_features.values()) > sum(matched.values()):
                    matched = score.matched_features
                for feature, weight in score.conflicting_features.items():
                    conflicting[feature] = min(conflicting.get(feature, 0), weight)
                candidate_evidence[existing_incident.id] = (existing_incident, matched, conflicting)
        best_score = float("-inf")
        runner_up_score = float("-inf")
        for existing_incident, matched, conflicting in candidate_evidence.values():
            score = EvidenceScore(
                round(sum(matched.values()) + sum(conflicting.values()), 2),
                matched,
                conflicting,
            )
            if score.total_score > best_score:
                runner_up_score = best_score
                best_score = score.total_score
                best = score
                candidate = existing_incident
            elif score.total_score > runner_up_score:
                runner_up_score = score.total_score
        ambiguous = has_ambiguous_candidates(best_score, runner_up_score)
        if candidate is not None and best.should_attach and not ambiguous:
            incident = candidate
        created = incident is None
        if incident is None:
            domain = Incident(
                tenant_id=event.tenant_id,
                title=alert.payload.get("summary") or alert.payload["alertname"],
                severity=alert.severity,
                correlation_key=key,
                opened_at=occurred_at,
            ).transition_phase(ProcessingPhase.READY_FOR_INVESTIGATION)
            incident = IncidentRecord(
                id=domain.id,
                tenant_id=domain.tenant_id,
                title=domain.title,
                status=domain.status.value,
                processing_phase=domain.processing_phase.value,
                severity=domain.severity,
                correlation_key=key,
                opened_at=domain.opened_at,
                version=domain.version,
            )
            session.add(incident)
            session.flush()
        else:
            # Membership changes invalidate evidence even when severity/time stay identical.
            incident = session.scalars(select(IncidentRecord).where(
                IncidentRecord.tenant_id == event.tenant_id, IncidentRecord.id == incident.id,
            ).with_for_update().execution_options(populate_existing=True)).one()
            if int(alert.severity[1]) < int(incident.severity[1]):
                incident.severity = alert.severity
            if occurred_at < incident.opened_at:
                incident.opened_at = occurred_at
            incident.version += 1
            incident.updated_at = now

        session.add(
            CorrelationDecisionRecord(
                id=uuid4(),
                tenant_id=event.tenant_id,
                alert_id=alert.id,
                candidate_incident_id=candidate.id if candidate else None,
                result_incident_id=incident.id,
                decision=("CREATE_NEW_AMBIGUOUS" if ambiguous else "CREATE_NEW")
                if created
                else "ATTACH",
                total_score=best.total_score,
                matched_features=best.matched_features,
                conflicting_features=best.conflicting_features,
                rule_version=RULE_VERSION,
                decided_by="correlation-worker",
                decided_at=now,
            )
        )
        session.add(
            IncidentAlertRecord(
                id=uuid4(),
                tenant_id=event.tenant_id,
                incident_id=incident.id,
                alert_id=alert.id,
            )
        )
        session.add(
            IncidentTimelineRecord(
                id=uuid4(),
                tenant_id=event.tenant_id,
                incident_id=incident.id,
                event_type="ALERT_ATTACHED",
                summary=alert.payload["alertname"],
                details={
                    "alert_id": str(alert.id),
                    "alert_occurred_at": occurred_at.isoformat(),
                    "alert_received_at": normalized.received_at.isoformat(),
                    "correlation_score": best.total_score,
                    "rule_version": RULE_VERSION,
                },
            )
        )
        if created:
            session.add(
                OutboxEventRecord(
                    id=uuid4(),
                    tenant_id=event.tenant_id,
                    event_type="incident.created.v1",
                    payload={"incident_id": str(incident.id)},
                )
            )
        else:
            session.add(OutboxEventRecord(
                id=uuid4(), tenant_id=event.tenant_id, event_type="incident.updated.v1",
                payload={"incident_id": str(incident.id), "incident_version": incident.version},
            ))
        session.add(
            InboxMessageRecord(
                id=uuid4(),
                tenant_id=event.tenant_id,
                consumer="incident-correlation-v1",
                message_id=event.message_id,
            )
        )
    return True
