"""Request scoped failure facts; aggregate alerts are not request evidence."""

import base64
import binascii
import re
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import RequestFailureEventRecord, ServiceRecord

TRACE_ID = re.compile(r"^[a-f0-9]{32}$")
SPAN_ID = re.compile(r"^[a-f0-9]{16}$")
CODE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
SERVICE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")


class RequestFailureEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    source_event_id: UUID
    service: str = Field(max_length=80)
    trace_id: str = Field(min_length=32, max_length=32)
    span_id: str = Field(min_length=16, max_length=16)
    failure_code: str = Field(max_length=80)
    resource: str | None = Field(default=None, max_length=120)
    occurred_at: datetime

    @field_validator("service", "trace_id", "span_id", "failure_code")
    @classmethod
    def valid_identity(cls, value: str, info: ValidationInfo) -> str:
        patterns = {
            "service": SERVICE,
            "trace_id": TRACE_ID,
            "span_id": SPAN_ID,
            "failure_code": CODE,
        }
        pattern = patterns.get(info.field_name or "")
        if pattern is None or pattern.fullmatch(value) is None:
            raise ValueError(f"invalid {info.field_name}")
        return value

    @field_validator("occurred_at")
    @classmethod
    def aware_recent_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must have a timezone")
        now = datetime.now(UTC)
        if value < now - timedelta(minutes=10) or value > now + timedelta(seconds=30):
            raise ValueError("occurred_at is outside the accepted request event window")
        return value.astimezone(UTC)


class RequestFailureEventView(BaseModel):
    id: UUID
    source_event_id: UUID
    service: str
    trace_id: str
    span_id: str
    failure_code: str
    resource: str | None
    occurred_at: datetime
    provenance_status: str


class EventConflict(Exception):
    pass


class UnknownEventService(Exception):
    pass


class RequestEventSourceUnauthorized(Exception):
    pass


class RequestEventIdentityUnavailable(Exception):
    pass


def authorize_request_event_source(
    settings: Settings, authorization: str | None, service: str
) -> None:
    tokens = settings.request_event_service_tokens
    if service not in tokens:
        raise RequestEventSourceUnauthorized
    token = tokens[service]
    if not token:
        raise RequestEventIdentityUnavailable
    if authorization is None or not compare_digest(authorization, f"Bearer {token}"):
        raise RequestEventSourceUnauthorized


def _view(row: RequestFailureEventRecord, status: str) -> RequestFailureEventView:
    return RequestFailureEventView(
        id=row.id,
        source_event_id=row.source_event_id,
        service=row.service_name,
        trace_id=row.trace_id,
        span_id=row.span_id,
        failure_code=row.failure_code,
        resource=row.resource_name,
        occurred_at=row.occurred_at,
        provenance_status=status,
    )


def record_request_event(
    settings: Settings, body: RequestFailureEventInput
) -> RequestFailureEventView:
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        known = session.scalar(
            select(ServiceRecord.id).where(
                ServiceRecord.tenant_id == tenant_id,
                ServiceRecord.slug == body.service,
            )
        )
        if known is None:
            raise UnknownEventService
        created = session.execute(
            insert(RequestFailureEventRecord).values(
                tenant_id=tenant_id,
                source_event_id=body.source_event_id,
                service_name=body.service,
                trace_id=body.trace_id,
                span_id=body.span_id,
                failure_code=body.failure_code,
                resource_name=body.resource,
                occurred_at=body.occurred_at,
            ).on_conflict_do_nothing(
                constraint="uq_request_failure_source_event"
            ).returning(RequestFailureEventRecord.id)
        ).scalar_one_or_none()
        row = session.scalar(
            select(RequestFailureEventRecord).where(
                RequestFailureEventRecord.tenant_id == tenant_id,
                RequestFailureEventRecord.service_name == body.service,
                RequestFailureEventRecord.source_event_id == body.source_event_id,
            )
        )
        assert row is not None
        if created is None and (
            row.trace_id != body.trace_id
            or row.span_id != body.span_id
            or row.failure_code != body.failure_code
            or row.resource_name != body.resource
            or row.occurred_at != body.occurred_at
        ):
            raise EventConflict
        return _view(row, "SOURCE_ASSERTED")


def get_request_event(settings: Settings, event_id: UUID) -> RequestFailureEventView | None:
    with session_for(settings.database_url) as session:
        row = session.scalar(
            select(RequestFailureEventRecord).where(
                RequestFailureEventRecord.tenant_id == settings.dev_tenant_id,
                RequestFailureEventRecord.id == event_id,
            )
        )
        return _view(row, "SOURCE_ASSERTED") if row else None


def events_for_trace(settings: Settings, trace_id: str) -> list[RequestFailureEventView]:
    if TRACE_ID.fullmatch(trace_id) is None:
        raise ValueError("invalid trace ID")
    with session_for(settings.database_url) as session:
        rows = session.scalars(
            select(RequestFailureEventRecord).where(
                RequestFailureEventRecord.tenant_id == settings.dev_tenant_id,
                RequestFailureEventRecord.trace_id == trace_id,
            ).order_by(RequestFailureEventRecord.occurred_at, RequestFailureEventRecord.id)
            .limit(50)
        ).all()
        return [_view(row, "SOURCE_ASSERTED") for row in rows]


def verify_request_event(settings: Settings, event_id: UUID) -> RequestFailureEventView | None:
    event = get_request_event(settings, event_id)
    if event is None:
        return None
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(f"{settings.tempo_url.rstrip('/')}/api/traces/{event.trace_id}")
        if response.status_code == 404:
            event.provenance_status = "TRACE_NOT_READY"
            return event
        response.raise_for_status()
        batches = response.json().get("batches", [])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        event.provenance_status = "TRACE_UNAVAILABLE"
        return event
    event.provenance_status = trace_fact_status(batches, event)
    return event


def trace_fact_status(batches: list[dict], event: RequestFailureEventView) -> str:
    """Verify the exact service span carries this failure fact, not just the trace ID."""
    for batch in batches:
        service = next(
            (item.get("value", {}).get("stringValue")
             for item in batch.get("resource", {}).get("attributes", [])
             if item.get("key") == "service.name"),
            None,
        )
        if service != event.service:
            continue
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                try:
                    span_id = base64.b64decode(span.get("spanId", ""), validate=True).hex()
                except (binascii.Error, ValueError, TypeError):
                    continue
                if span_id != event.span_id:
                    continue
                attributes = {
                    item.get("key"): item.get("value", {}).get("stringValue")
                    for item in span.get("attributes", [])
                }
                return (
                    "VERIFIED_SPAN_FACT"
                    if attributes.get("aegisops.failure_code") == event.failure_code
                    and attributes.get("aegisops.resource") == (event.resource or "")
                    else "SPAN_FACT_MISMATCH"
                )
    return "SPAN_NOT_FOUND"
