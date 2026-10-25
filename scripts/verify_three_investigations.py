"""Replay the three real Lab incidents through persistent read-only investigation."""

from collections import Counter

from sqlalchemy import func, select

from aegisops.application.investigation.service import investigate_incident, latest_report
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    IncidentAlertRecord,
    IncidentTimelineRecord,
)

CASES = (
    ("PaymentRedisUnavailable", "redis_unavailable"),
    ("OrderDbPoolExhausted", "order_db_pool_exhaustion"),
    ("OrderBadDeployment", "bad_order_deployment"),
)


def main() -> None:
    settings = Settings()
    for alert_name, expected in CASES:
        with session_for(settings.database_url) as session:
            incident_id = session.scalar(
                select(IncidentAlertRecord.incident_id)
                .join(
                    AlertRecord,
                    (AlertRecord.tenant_id == IncidentAlertRecord.tenant_id)
                    & (AlertRecord.id == IncidentAlertRecord.alert_id),
                )
                .where(
                    AlertRecord.tenant_id == settings.dev_tenant_id,
                    AlertRecord.payload["alertname"].astext == alert_name,
                )
                .order_by(AlertRecord.occurred_at.desc())
                .limit(1)
            )
            assert incident_id is not None, f"run the {alert_name} Lab scenario first"
            before = session.scalar(
                select(func.count())
                .select_from(IncidentTimelineRecord)
                .where(
                    IncidentTimelineRecord.tenant_id == settings.dev_tenant_id,
                    IncidentTimelineRecord.incident_id == incident_id,
                    IncidentTimelineRecord.event_type == "INVESTIGATION_REPORTED",
                )
            )
        assert investigate_incident(settings, settings.dev_tenant_id, incident_id) == "SUPPORTED"
        first = latest_report(settings, settings.dev_tenant_id, incident_id)
        assert first is not None
        assert first.primary_hypothesis == expected
        assert first.confidence >= 0.6
        assert "METRIC" in {item.source_type for item in first.evidence}
        assert all(
            item.tool_name and item.tool_version and item.collected_at for item in first.evidence
        )
        assert investigate_incident(settings, settings.dev_tenant_id, incident_id) == "SUPPORTED"
        second = latest_report(settings, settings.dev_tenant_id, incident_id)
        assert second is not None and second.id == first.id
        with session_for(settings.database_url) as session:
            after = session.scalar(
                select(func.count())
                .select_from(IncidentTimelineRecord)
                .where(
                    IncidentTimelineRecord.tenant_id == settings.dev_tenant_id,
                    IncidentTimelineRecord.incident_id == incident_id,
                    IncidentTimelineRecord.event_type == "INVESTIGATION_REPORTED",
                )
            )
        assert after == before + (0 if before else 1)
        print(
            f"PASS {expected}: report={first.id} confidence={first.confidence} "
            f"sources={dict(Counter(item.source_type for item in first.evidence))}"
        )


if __name__ == "__main__":
    main()
