from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.application.contracts import AlertmanagerWebhook, IngestResult
from aegisops.application.normalization import normalize_prometheus
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import AlertRecord, OutboxEventRecord


def ingest_alerts(settings: Settings, notification: AlertmanagerWebhook) -> IngestResult:
    accepted = 0
    duplicates = 0
    with session_for(settings.database_url) as session, session.begin():
        for item in notification.alerts:
            normalized = normalize_prometheus(item)
            external_id = normalized.source_event_id
            # Keep a small, known set of external fields in the control-plane database.
            payload = {
                "alertname": normalized.alert_name,
                "service": normalized.service_id,
                "resource": normalized.resource_id,
                "change_id": normalized.change_id,
                "signal_type": normalized.signal_type,
                "summary": normalized.summary,
                "labels": normalized.labels,
            }
            alert_id = uuid4()
            statement = (
                insert(AlertRecord)
                .values(
                    id=alert_id,
                    tenant_id=settings.dev_tenant_id,
                    source="prometheus",
                    external_id=external_id,
                    source_event_id=normalized.source_event_id,
                    schema_version=normalized.schema_version,
                    fingerprint=item.fingerprint,
                    service_name=normalized.service_id,
                    severity=normalized.severity,
                    status=item.status.upper(),
                    starts_at=normalized.occurred_at,
                    occurred_at=normalized.occurred_at,
                    ends_at=item.endsAt if item.status == "resolved" else None,
                    payload=payload,
                    received_at=normalized.received_at,
                )
                .on_conflict_do_nothing(constraint="uq_alerts_external")
                .returning(AlertRecord.id)
            )
            inserted = session.execute(statement).scalar_one_or_none()
            if inserted is None:
                duplicates += 1
                if item.status == "resolved":
                    existing = session.scalar(
                        select(AlertRecord).where(
                            AlertRecord.tenant_id == settings.dev_tenant_id,
                            AlertRecord.source == "prometheus",
                            AlertRecord.external_id == external_id,
                        )
                    )
                    if existing is not None:
                        existing.status = "RESOLVED"
                        existing.ends_at = item.endsAt
                        existing.updated_at = datetime.now(UTC)
                        existing.version += 1
                continue
            accepted += 1
            if item.status == "firing":
                session.add(
                    OutboxEventRecord(
                        id=uuid4(),
                        tenant_id=settings.dev_tenant_id,
                        event_type="alert.received.v1",
                        payload={"alert_id": str(alert_id)},
                    )
                )
    return IngestResult(accepted=accepted, duplicates=duplicates)
