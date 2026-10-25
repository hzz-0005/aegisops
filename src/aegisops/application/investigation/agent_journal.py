"""Reserve before I/O. A interrupted or ambiguous provider call is never repeated."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentAlertRecord,
    IncidentRecord,
    InvestigationAgentRunRecord,
)

from .agent_contracts import AgentTrace


class AgentAlreadyRunning(RuntimeError):
    pass


def snapshot_hash(snapshot: dict) -> str:
    value = {"version": snapshot["version"],
             "members": sorted(str(item["id"]) for item in snapshot["alerts"])}
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class DatabaseAgentJournal:
    def __init__(self, settings: Settings, snapshot: dict) -> None:
        self.settings, self.snapshot = settings, snapshot
        self.resumed = False

    def claim(self) -> AgentTrace:
        now = datetime.now(UTC)
        trace = AgentTrace(
            run_id=uuid4(), model_name=self.settings.llm_model, started_at=now,
            deadline_at=now + timedelta(seconds=self.settings.investigation_agent_seconds),
        )
        s = self.snapshot
        with session_for(self.settings.database_url) as session, session.begin():
            created = session.execute(insert(InvestigationAgentRunRecord).values(
                id=trace.run_id, tenant_id=s["tenant_id"], incident_id=s["id"],
                source_incident_version=s["version"], snapshot_hash=snapshot_hash(s),
                status="RUNNING", model_calls=0, tool_calls=0, trace=trace.model_dump(mode="json"),
            ).on_conflict_do_nothing(constraint="uq_agent_run_incident_version")
                .returning(InvestigationAgentRunRecord.id)).scalar_one_or_none()
            if created is not None:
                return trace
            row = session.scalar(select(InvestigationAgentRunRecord).where(
                InvestigationAgentRunRecord.tenant_id == s["tenant_id"],
                InvestigationAgentRunRecord.incident_id == s["id"],
                InvestigationAgentRunRecord.source_incident_version == s["version"],
            ).with_for_update())
            assert row is not None
            trace = AgentTrace.model_validate(row.trace)
            if row.status == "RUNNING" and trace.deadline_at > now:
                raise AgentAlreadyRunning("agent run already owns this incident version")
            self.resumed = True
            if row.snapshot_hash != snapshot_hash(s):
                trace.stop_reason = "VERSION_CHANGED"
            elif not trace.stop_reason:
                trace.stop_reason = "INTERRUPTED_UNKNOWN_RESULT"
            row.status = "STOPPED"
            row.trace = trace.model_dump(mode="json")
            return trace

    def fresh(self) -> bool:
        s = self.snapshot
        with session_for(self.settings.database_url) as session:
            # One SQL statement gives version and membership from one MVCC snapshot.
            rows = session.execute(select(IncidentRecord.version, IncidentAlertRecord.alert_id)
                .outerjoin(IncidentAlertRecord,
                           (IncidentAlertRecord.tenant_id == IncidentRecord.tenant_id)
                           & (IncidentAlertRecord.incident_id == IncidentRecord.id))
                .where(IncidentRecord.tenant_id == s["tenant_id"],
                       IncidentRecord.id == s["id"])).all()
        return (bool(rows) and rows[0][0] == s["version"]
                and {str(row[1]) for row in rows if row[1] is not None}
                == {str(a["id"]) for a in s["alerts"]})

    def save(self, trace: AgentTrace) -> None:
        with session_for(self.settings.database_url) as session, session.begin():
            row = session.scalar(select(InvestigationAgentRunRecord).where(
                InvestigationAgentRunRecord.id == trace.run_id,
                InvestigationAgentRunRecord.tenant_id == self.snapshot["tenant_id"],
            ).with_for_update())
            assert row is not None
            if row.status != "RUNNING":
                raise AgentAlreadyRunning("run no longer owns its reservation")
            row.model_calls, row.tool_calls = trace.model_calls, trace.tool_calls
            row.trace = trace.model_dump(mode="json")
            row.status = "STOPPED" if trace.stop_reason else "RUNNING"
            row.updated_at = datetime.now(UTC)
            row.version += 1
