"""Check conservative handling of an uncertain start older than the safety window."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from aegisops.application.incident_workflow import reserve_workflow_start
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import IncidentRecord, IncidentWorkflowStartRecord


def main() -> None:
    settings = Settings()
    incident_id = uuid4()
    with session_for(settings.database_url) as session, session.begin():
        session.add(
            IncidentRecord(
                id=incident_id,
                tenant_id=settings.dev_tenant_id,
                title="Stale workflow start verification",
                status="OPEN",
                processing_phase="READY_FOR_INVESTIGATION",
                severity="P3",
                opened_at=datetime.now(UTC),
            )
        )
    try:
        assert reserve_workflow_start(settings, settings.dev_tenant_id, incident_id) == "START"
        with session_for(settings.database_url) as session, session.begin():
            row = session.scalar(
                select(IncidentWorkflowStartRecord).where(
                    IncidentWorkflowStartRecord.tenant_id == settings.dev_tenant_id,
                    IncidentWorkflowStartRecord.incident_id == incident_id,
                )
            )
            assert row is not None
            row.first_attempted_at = datetime.now(UTC) - timedelta(
                hours=settings.temporal_start_reconcile_after_hours + 1
            )
        assert (
            reserve_workflow_start(settings, settings.dev_tenant_id, incident_id)
            == "RECONCILIATION_REQUIRED"
        )
        assert (
            reserve_workflow_start(settings, settings.dev_tenant_id, incident_id)
            == "RECONCILIATION_REQUIRED"
        )
        print("PASS stale uncertain start is quarantined; no automatic restart")
    finally:
        with session_for(settings.database_url) as session, session.begin():
            row = session.scalar(
                select(IncidentWorkflowStartRecord).where(
                    IncidentWorkflowStartRecord.tenant_id == settings.dev_tenant_id,
                    IncidentWorkflowStartRecord.incident_id == incident_id,
                )
            )
            if row is not None:
                session.delete(row)
            incident = session.get(IncidentRecord, incident_id)
            if incident is not None:
                session.delete(incident)


if __name__ == "__main__":
    main()
