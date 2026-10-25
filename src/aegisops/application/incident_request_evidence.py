"""Operator selected request facts; this does not infer alert causation."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from aegisops.application.contracts import nonblank_reason
from aegisops.application.incident_lifecycle import IncidentNotFound, IncidentVersionConflict
from aegisops.application.request_events import verify_request_event
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentRecord,
    IncidentRequestEvidenceRecord,
    IncidentTimelineRecord,
    RequestFailureEventRecord,
)


class AttachRequestEvidenceInput(BaseModel):
    request_event_id: UUID
    expected_incident_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        return nonblank_reason(value)


class IncidentRequestEvidenceView(BaseModel):
    id: UUID
    incident_id: UUID
    request_event_id: UUID
    trace_id: str
    span_id: str
    service: str
    failure_code: str
    resource: str | None
    operator_id: str
    reason: str
    incident_version_at_link: int
    verification_status: str
    relationship_status: Literal["OPERATOR_SELECTED_CONTEXT"] = "OPERATOR_SELECTED_CONTEXT"
    created_at: datetime


class RequestEvidenceNotFound(Exception):
    pass


class RequestEvidenceNotVerified(Exception):
    pass


class RequestEvidenceConflict(Exception):
    pass


def _view(
    link: IncidentRequestEvidenceRecord, event: RequestFailureEventRecord
) -> IncidentRequestEvidenceView:
    return IncidentRequestEvidenceView(
        id=link.id,
        incident_id=link.incident_id,
        request_event_id=link.request_event_id,
        trace_id=event.trace_id,
        span_id=event.span_id,
        service=event.service_name,
        failure_code=event.failure_code,
        resource=event.resource_name,
        operator_id=link.operator_id,
        reason=link.reason,
        incident_version_at_link=link.incident_version_at_link,
        verification_status=link.verification_status,
        created_at=link.created_at,
    )


def attach_request_evidence(
    settings: Settings,
    incident_id: UUID,
    body: AttachRequestEvidenceInput,
    *,
    operator_id: str,
) -> IncidentRequestEvidenceView:
    verified = verify_request_event(settings, body.request_event_id)
    if verified is None:
        raise RequestEvidenceNotFound
    if verified.provenance_status != "VERIFIED_SPAN_FACT":
        raise RequestEvidenceNotVerified

    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        incident = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.tenant_id == tenant_id,
                IncidentRecord.id == incident_id,
            ).with_for_update()
        )
        if incident is None:
            raise IncidentNotFound
        if incident.version != body.expected_incident_version:
            raise IncidentVersionConflict
        event = session.scalar(
            select(RequestFailureEventRecord).where(
                RequestFailureEventRecord.tenant_id == tenant_id,
                RequestFailureEventRecord.id == body.request_event_id,
            )
        )
        if event is None:
            raise RequestEvidenceNotFound
        existing = session.scalar(
            select(IncidentRequestEvidenceRecord).where(
                IncidentRequestEvidenceRecord.tenant_id == tenant_id,
                IncidentRequestEvidenceRecord.incident_id == incident_id,
                IncidentRequestEvidenceRecord.request_event_id == body.request_event_id,
            )
        )
        if existing is not None:
            if existing.reason != body.reason or existing.operator_id != operator_id:
                raise RequestEvidenceConflict
            return _view(existing, event)
        link = IncidentRequestEvidenceRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            incident_id=incident_id,
            request_event_id=body.request_event_id,
            operator_id=operator_id,
            reason=body.reason,
            incident_version_at_link=incident.version,
            verification_status="VERIFIED_SPAN_FACT",
        )
        session.add(link)
        session.flush()
        session.refresh(link)
        session.add(
            IncidentTimelineRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="REQUEST_EVIDENCE_ATTACHED",
                summary="操作员附加已核验的请求失败事实",
                details={
                    "request_event_id": str(body.request_event_id),
                    "trace_id": event.trace_id,
                    "span_id": event.span_id,
                    "operator_id": operator_id,
                    "reason": body.reason,
                    "incident_version_at_link": incident.version,
                    "verification_status": "VERIFIED_SPAN_FACT",
                    "relationship_status": "OPERATOR_SELECTED_CONTEXT",
                },
            )
        )
        return _view(link, event)


def list_request_evidence(
    settings: Settings, incident_id: UUID
) -> list[IncidentRequestEvidenceView]:
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session:
        exists = session.scalar(
            select(IncidentRecord.id).where(
                IncidentRecord.tenant_id == tenant_id,
                IncidentRecord.id == incident_id,
            )
        )
        if exists is None:
            raise IncidentNotFound
        rows = session.execute(
            select(IncidentRequestEvidenceRecord, RequestFailureEventRecord)
            .join(
                RequestFailureEventRecord,
                (RequestFailureEventRecord.tenant_id == IncidentRequestEvidenceRecord.tenant_id)
                & (RequestFailureEventRecord.id == IncidentRequestEvidenceRecord.request_event_id),
            )
            .where(
                IncidentRequestEvidenceRecord.tenant_id == tenant_id,
                IncidentRequestEvidenceRecord.incident_id == incident_id,
            )
            .order_by(IncidentRequestEvidenceRecord.created_at, IncidentRequestEvidenceRecord.id)
        ).all()
        return [_view(link, event) for link, event in rows]
