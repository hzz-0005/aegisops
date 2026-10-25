"""Diagnostic overlap of broad-window trace observations in investigation reports."""

import re
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentRecord,
    InvestigationEvidenceRecord,
    InvestigationReportRecord,
)

from .service import latest_report

RULE_VERSION = "trace-observation-overlap-v2"
ERROR_TRACE_KINDS = {"ERROR_SPAN", "ERROR_SPAN_ACTIVITY"}
TRACE_SOURCE_REF = re.compile(r"tempo:[a-fA-F0-9]{16,32}\Z")


class SharedTraceEvidence(BaseModel):
    source_ref: str
    source_evidence_id: UUID
    other_evidence_id: UUID


class TraceOverlapObservation(BaseModel):
    other_incident_id: UUID
    rule_version: str = RULE_VERSION
    match_basis: str = "SERVICE_TIME_WINDOW"
    source_report_id: UUID
    other_report_id: UUID
    overlapping_traces: list[SharedTraceEvidence]
    interpretation: str = (
        "两份调查报告在服务时间窗内读取了同一失败追踪；"
        "未证明告警共享请求、事故相关或根因相同。"
    )


def _trace_evidence(evidence: object) -> bool:
    source_ref = getattr(evidence, "source_ref", None)
    return (
        getattr(evidence, "source_type", None) == "TRACE"
        and getattr(evidence, "kind", None) in ERROR_TRACE_KINDS
        and isinstance(source_ref, str)
        and TRACE_SOURCE_REF.fullmatch(source_ref) is not None
    )


def trace_overlaps(
    settings: Settings, tenant_id: UUID, incident_id: UUID
) -> list[TraceOverlapObservation]:
    """Find report overlap only; Tempo search is not anchored to an alert's request."""
    report = latest_report(settings, tenant_id, incident_id)
    if report is None or report.is_stale:
        return []
    source = {item.source_ref: item for item in report.evidence if _trace_evidence(item)}
    if not source:
        return []
    with session_for(settings.database_url) as session:
        matches = session.execute(
            select(InvestigationReportRecord, InvestigationEvidenceRecord)
            .join(
                InvestigationEvidenceRecord,
                (InvestigationEvidenceRecord.tenant_id == InvestigationReportRecord.tenant_id)
                & (InvestigationEvidenceRecord.report_id == InvestigationReportRecord.id),
            )
            .join(
                IncidentRecord,
                (IncidentRecord.tenant_id == InvestigationReportRecord.tenant_id)
                & (IncidentRecord.id == InvestigationReportRecord.incident_id),
            )
            .where(
                InvestigationReportRecord.tenant_id == tenant_id,
                InvestigationReportRecord.incident_id != incident_id,
                InvestigationReportRecord.source_incident_version == IncidentRecord.version,
                InvestigationEvidenceRecord.source_type == "TRACE",
                InvestigationEvidenceRecord.kind.in_(ERROR_TRACE_KINDS),
                InvestigationEvidenceRecord.source_ref.in_(source),
            )
            .order_by(InvestigationReportRecord.incident_id, InvestigationEvidenceRecord.source_ref)
            .limit(200)
        ).all()
    grouped: dict[UUID, TraceOverlapObservation] = {}
    for other_report, evidence in matches:
        overlap = grouped.get(other_report.incident_id)
        if overlap is None:
            overlap = TraceOverlapObservation(
                other_incident_id=other_report.incident_id,
                source_report_id=report.id,
                other_report_id=other_report.id,
                overlapping_traces=[],
            )
            grouped[other_report.incident_id] = overlap
        if len(overlap.overlapping_traces) < 8 and not any(
            item.source_ref == evidence.source_ref for item in overlap.overlapping_traces
        ):
            overlap.overlapping_traces.append(
                SharedTraceEvidence(
                    source_ref=evidence.source_ref,
                    source_evidence_id=source[evidence.source_ref].id,
                    other_evidence_id=evidence.id,
                )
            )
    return sorted(
        grouped.values(),
        key=lambda item: (-len(item.overlapping_traces), str(item.other_incident_id)),
    )[:20]
