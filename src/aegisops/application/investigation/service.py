"""Run a bounded read-only graph and atomically persist its report once per version."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentAlertRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    InvestigationEvidenceRecord,
    InvestigationReportRecord,
)

from .contracts import Evidence, Hypothesis, InvestigationReport
from .graph import build_investigation_graph


def latest_report(
    settings: Settings, tenant_id: UUID, incident_id: UUID
) -> InvestigationReport | None:
    with session_for(settings.database_url) as session:
        row = session.scalar(
            select(InvestigationReportRecord)
            .where(
                InvestigationReportRecord.tenant_id == tenant_id,
                InvestigationReportRecord.incident_id == incident_id,
            )
            .order_by(
                InvestigationReportRecord.source_incident_version.desc(),
                InvestigationReportRecord.completed_at.desc(),
            )
            .limit(1)
        )
        if row is None:
            return None
        current_version = session.scalar(
            select(IncidentRecord.version).where(
                IncidentRecord.tenant_id == tenant_id,
                IncidentRecord.id == incident_id,
            )
        )
        evidence_rows = session.scalars(
            select(InvestigationEvidenceRecord)
            .where(
                InvestigationEvidenceRecord.tenant_id == tenant_id,
                InvestigationEvidenceRecord.report_id == row.id,
            )
            .order_by(InvestigationEvidenceRecord.created_at, InvestigationEvidenceRecord.id)
        ).all()
        current_members = set(session.scalars(select(IncidentAlertRecord.alert_id).where(
            IncidentAlertRecord.tenant_id == tenant_id,
            IncidentAlertRecord.incident_id == incident_id,
        )).all())
        recorded_members = {item.source_ref.removeprefix("alert:") for item in evidence_rows
                            if item.kind == "NORMALIZED_ALERT" and item.source_type == "ALERT"}
        membership_stale = recorded_members != {str(member) for member in current_members}
        report = InvestigationReport.model_validate(
            dict(
                id=row.id,
                tenant_id=row.tenant_id,
                incident_id=row.incident_id,
                source_incident_version=row.source_incident_version,
                current_incident_version=current_version,
                is_stale=current_version != row.source_incident_version or membership_stale,
                status=row.status,
                graph_version=row.graph_version,
                summary=row.summary,
                primary_hypothesis=row.primary_hypothesis,
                confidence=float(row.confidence),
                hypotheses=[Hypothesis.model_validate(item) for item in row.hypotheses],
                evidence=[
                    Evidence.model_validate(
                        dict(
                            id=item.id,
                            source_type=item.source_type,
                            source_ref=item.source_ref,
                            kind=item.kind,
                            subject=item.subject,
                            polarity=item.polarity,
                            observed_at=item.observed_at,
                            collected_at=item.collected_at,
                            tool_name=item.tool_name,
                            tool_version=item.tool_version,
                            payload=item.payload,
                        )
                    )
                    for item in evidence_rows
                ],
                limitations=row.limitations,
                completed_at=row.completed_at,
                agent_trace=row.agent_trace,
                candidate_verifications=(row.agent_trace or {}).get("candidate_verifications", []),
                root_cause_gates=(row.agent_trace or {}).get("root_cause_gates", []),
            )
        )
        if report.is_stale:
            report.candidate_verifications = [v.model_copy(update={
                "status": "INSUFFICIENT", "reason_codes": ["SNAPSHOT_CHANGED"],
            }) for v in report.candidate_verifications]
            report.root_cause_gates = [g.model_copy(update={
                "decision": "GATE_NOT_PASSED",
                "blocking_reasons": [*g.blocking_reasons, "SNAPSHOT_CHANGED"],
            }) for g in report.root_cause_gates]
        return report


def investigate_incident(settings: Settings, tenant_id: UUID, incident_id: UUID) -> str:
    with session_for(settings.database_url) as session:
        incident_version = session.scalar(
            select(IncidentRecord.version).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
    if incident_version is None:
        return "SKIPPED_MISSING"
    existing = latest_report(settings, tenant_id, incident_id)
    if existing is not None and existing.source_incident_version == incident_version:
        return "STALE_REPORT" if existing.is_stale else existing.status
    graph = build_investigation_graph(settings)
    state = graph.invoke({"tenant_id": tenant_id, "incident_id": incident_id})
    report: InvestigationReport = state["report"]
    with session_for(settings.database_url) as session, session.begin():
        created = session.execute(
            insert(InvestigationReportRecord)
            .values(
                id=report.id,
                tenant_id=report.tenant_id,
                incident_id=report.incident_id,
                source_incident_version=report.source_incident_version,
                status=report.status,
                graph_version=report.graph_version,
                summary=report.summary,
                primary_hypothesis=report.primary_hypothesis,
                confidence=report.confidence,
                hypotheses=[item.model_dump(mode="json") for item in report.hypotheses],
                limitations=report.limitations,
                completed_at=report.completed_at,
                agent_trace=report.agent_trace.model_dump(mode="json")
                if report.agent_trace else None,
            )
            .on_conflict_do_nothing(constraint="uq_investigation_report_incident_version")
            .returning(InvestigationReportRecord.id)
        ).scalar_one_or_none()
        if created is None:
            winner = session.scalar(
                select(InvestigationReportRecord.status).where(
                    InvestigationReportRecord.tenant_id == tenant_id,
                    InvestigationReportRecord.incident_id == incident_id,
                    InvestigationReportRecord.source_incident_version
                    == report.source_incident_version,
                )
            )
            assert winner is not None
            return winner
        session.add_all(
            InvestigationEvidenceRecord(
                id=item.id,
                tenant_id=tenant_id,
                report_id=report.id,
                source_type=item.source_type,
                source_ref=item.source_ref,
                kind=item.kind,
                subject=item.subject,
                polarity=item.polarity,
                observed_at=item.observed_at,
                collected_at=item.collected_at,
                tool_name=item.tool_name,
                tool_version=item.tool_version,
                payload=item.payload,
            )
            for item in report.evidence
        )
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="INVESTIGATION_REPORTED",
                summary=f"Read-only investigation: {report.status}",
                details={
                    "report_id": str(report.id),
                    "graph_version": report.graph_version,
                    "status": report.status,
                    "primary_hypothesis": report.primary_hypothesis,
                    "confidence": report.confidence,
                    "evidence_count": len(report.evidence),
                    "source_incident_version": report.source_incident_version,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
        )
    return report.status
