"""Verify durable reservation/retry/tenant fencing against the real local database, no LLM."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select

from aegisops.application.investigation.agent_contracts import AgentStep
from aegisops.application.investigation.agent_journal import (
    AgentAlreadyRunning,
    DatabaseAgentJournal,
)
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import IncidentRecord, InvestigationAgentRunRecord


def main() -> None:
    settings = Settings()
    require_dev_runtime(settings)
    incident_id = uuid4()
    snapshot = {"id": incident_id, "tenant_id": settings.dev_tenant_id, "version": 1, "alerts": []}
    with session_for(settings.database_url) as session, session.begin():
        session.add(
            IncidentRecord(
                id=incident_id,
                tenant_id=settings.dev_tenant_id,
                title="Offline journal verification",
                status="OPEN",
                processing_phase="NEW",
                severity="S3",
                opened_at=datetime.now(UTC),
            )
        )
    try:
        journal = DatabaseAgentJournal(settings, snapshot)
        trace = journal.claim()
        trace.model_calls = 1
        trace.steps.append(
            AgentStep(number=1, phase="MODEL", status="RESERVED", input_hash="a" * 64)
        )
        journal.save(trace)
        assert journal.fresh()
        try:
            DatabaseAgentJournal(settings, snapshot).claim()
        except AgentAlreadyRunning:
            pass
        else:
            raise AssertionError("concurrent claimant must not reset or take budget")
        # Simulate process death after reservation and expiry, without waiting or another API call.
        trace.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        journal.save(trace)
        retry = DatabaseAgentJournal(settings, snapshot)
        recovered = retry.claim()
        assert retry.resumed and recovered.model_calls == 1
        assert recovered.stop_reason == "INTERRUPTED_UNKNOWN_RESULT"
        assert recovered.steps[0].status == "RESERVED"
        try:
            journal.save(trace)
        except AgentAlreadyRunning:
            pass
        else:
            raise AssertionError("expired owner must not overwrite recovered run")
        foreign_snapshot = {**snapshot, "tenant_id": uuid4()}
        assert not DatabaseAgentJournal(settings, foreign_snapshot).fresh()
        with session_for(settings.database_url) as session, session.begin():
            row = session.scalar(select(IncidentRecord).where(IncidentRecord.id == incident_id))
            assert row is not None
            row.version += 1
        assert not journal.fresh()
        print(
            "PASS actual PostgreSQL: reservation, concurrent ownership, unknown retry, "
            "late-owner fencing, tenant isolation, version drift; zero model calls"
        )
    finally:
        # Remove only the UUID created by this verifier, never historical user incidents.
        with session_for(settings.database_url) as session, session.begin():
            session.execute(
                delete(InvestigationAgentRunRecord).where(
                    InvestigationAgentRunRecord.tenant_id == settings.dev_tenant_id,
                    InvestigationAgentRunRecord.incident_id == incident_id,
                )
            )
            session.execute(
                delete(IncidentRecord).where(
                    IncidentRecord.tenant_id == settings.dev_tenant_id,
                    IncidentRecord.id == incident_id,
                )
            )


if __name__ == "__main__":
    main()
