"""Read-only cohorts of exact persisted request events, without alert attribution."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from aegisops.application.request_events import RequestFailureEventView
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import RequestFailureEventRecord

MAX_WINDOW_EVENTS = 1000


class RequestFailureMember(BaseModel):
    id: UUID
    source_event_id: UUID
    trace_id: str
    span_id: str
    occurred_at: datetime


class RequestFailureCohort(BaseModel):
    service: str
    failure_code: str
    resource: str | None
    members: list[RequestFailureMember]
    distinct_trace_count: int
    first_seen: datetime
    last_seen: datetime
    membership_status: Literal["PERSISTED_EVENT_SET"] = "PERSISTED_EVENT_SET"
    alert_binding_status: Literal["UNBOUND"] = "UNBOUND"


class CohortWindowTooLarge(Exception):
    pass


def group_request_failures(
    events: list[RequestFailureEventView], *, min_distinct_traces: int
) -> list[RequestFailureCohort]:
    grouped: dict[tuple[str, str, str | None], list[RequestFailureEventView]] = defaultdict(list)
    for event in events:
        grouped[(event.service, event.failure_code, event.resource)].append(event)
    result: list[RequestFailureCohort] = []
    for (service, code, resource), members in grouped.items():
        traces = {event.trace_id for event in members}
        if len(traces) < min_distinct_traces:
            continue
        ordered = sorted(members, key=lambda event: (event.occurred_at, event.id))
        result.append(
            RequestFailureCohort(
                service=service,
                failure_code=code,
                resource=resource,
                members=[
                    RequestFailureMember(
                        id=event.id,
                        source_event_id=event.source_event_id,
                        trace_id=event.trace_id,
                        span_id=event.span_id,
                        occurred_at=event.occurred_at,
                    )
                    for event in ordered
                ],
                distinct_trace_count=len(traces),
                first_seen=ordered[0].occurred_at,
                last_seen=ordered[-1].occurred_at,
            )
        )
    return sorted(result, key=lambda cohort: cohort.last_seen, reverse=True)


def recent_request_failure_cohorts(
    settings: Settings,
    *,
    window_minutes: int,
    min_distinct_traces: int,
    service: str | None = None,
) -> list[RequestFailureCohort]:
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    with session_for(settings.database_url) as session:
        statement = select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == settings.dev_tenant_id,
            RequestFailureEventRecord.occurred_at >= since,
        )
        if service:
            statement = statement.where(RequestFailureEventRecord.service_name == service)
        rows = session.scalars(
            statement.order_by(
                RequestFailureEventRecord.occurred_at.desc(), RequestFailureEventRecord.id
            ).limit(MAX_WINDOW_EVENTS + 1)
        ).all()
    if len(rows) > MAX_WINDOW_EVENTS:
        raise CohortWindowTooLarge
    events = [
        RequestFailureEventView(
            id=row.id,
            source_event_id=row.source_event_id,
            service=row.service_name,
            trace_id=row.trace_id,
            span_id=row.span_id,
            failure_code=row.failure_code,
            resource=row.resource_name,
            occurred_at=row.occurred_at,
            provenance_status="SOURCE_ASSERTED",
        )
        for row in rows
    ]
    return group_request_failures(events, min_distinct_traces=min_distinct_traces)
