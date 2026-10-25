"""Exact request ancestry, with no automatic aggregate-alert or root attribution."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from aegisops.application.request_events import RequestFailureEventView, _view, trace_fact_status
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import RequestFailureEventRecord

TRACE = re.compile(r"^[a-f0-9]{32}$")
MAX_EVENTS, MAX_SPANS, MAX_PATH = 16, 256, 16
MAX_CANDIDATES = 4
CLOCK_TOLERANCE_NS = 1_000_000


class RequestLinkUnverifiable(ValueError):
    pass


class RequestFailureLink(BaseModel):
    trace_id: str
    upstream_event_id: UUID
    downstream_event_id: UUID
    upstream_service: str
    downstream_service: str
    downstream_failure_code: str
    downstream_resource: str | None
    span_path: list[str]
    observed_at: datetime
    relationship_status: Literal["VERIFIED_REQUEST_ANCESTRY"] = "VERIFIED_REQUEST_ANCESTRY"
    root_cause_status: Literal["UNDETERMINED"] = "UNDETERMINED"
    alert_binding_status: Literal["UNBOUND"] = "UNBOUND"


@dataclass(frozen=True)
class Span:
    id: str
    parent: str | None
    service: str
    kind: str
    start: int
    end: int
    http_status: int | None


def _binary(value: object, size: int) -> str:
    if not isinstance(value, str):
        raise RequestLinkUnverifiable("invalid binary identifier")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestLinkUnverifiable("invalid binary identifier") from exc
    if len(raw) != size or not any(raw):
        raise RequestLinkUnverifiable("invalid binary identifier size")
    return raw.hex()


def _spans(batches: list[dict], trace_id: str) -> dict[str, Span]:
    spans: dict[str, Span] = {}
    for batch in batches:
        services = [a.get("value", {}).get("stringValue")
                    for a in batch.get("resource", {}).get("attributes", [])
                    if a.get("key") == "service.name"]
        if len(services) != 1 or not isinstance(services[0], str):
            raise RequestLinkUnverifiable("ambiguous service identity")
        for scope in batch.get("scopeSpans", []):
            for raw in scope.get("spans", []):
                if len(spans) >= MAX_SPANS:
                    raise RequestLinkUnverifiable("trace span limit exceeded")
                if _binary(raw.get("traceId"), 16) != trace_id:
                    raise RequestLinkUnverifiable("mixed trace identities")
                ident = _binary(raw.get("spanId"), 8)
                if ident in spans:
                    raise RequestLinkUnverifiable("duplicate span identity")
                parent = _binary(raw["parentSpanId"], 8) if raw.get("parentSpanId") else None
                try:
                    start, end = int(raw["startTimeUnixNano"]), int(raw["endTimeUnixNano"])
                    attributes = {a["key"]: a.get("value", {}) for a in raw.get("attributes", [])}
                    status_value = attributes.get("http.response.status_code",
                                                  attributes.get("http.status_code", {}))
                    status = int(status_value["intValue"]) if "intValue" in status_value else None
                except (KeyError, ValueError, TypeError) as exc:
                    raise RequestLinkUnverifiable("invalid span timing or response") from exc
                if start <= 0 or end < start:
                    raise RequestLinkUnverifiable("invalid span interval")
                spans[ident] = Span(ident, parent, services[0], raw.get("kind", ""),
                                    start, end, status)
    return spans


def _path(spans: dict[str, Span], upstream: str, downstream: str) -> list[Span] | None:
    path: list[Span] = []
    seen = set()
    current: str | None = downstream
    while current and len(path) < MAX_PATH:
        if current in seen or current not in spans:
            return None
        seen.add(current)
        path.append(spans[current])
        if current == upstream:
            return list(reversed(path))
        current = spans[current].parent
    return None


def links_for_events(events: list[RequestFailureEventView], batches: list[dict]
                     ) -> list[RequestFailureLink]:
    if not events:
        return []
    trace_id = events[0].trace_id
    if (len(events) > MAX_EVENTS or not TRACE.fullmatch(trace_id)
            or len({e.id for e in events}) != len(events)
            or any(e.trace_id != trace_id or e.occurred_at.utcoffset() is None for e in events)):
        raise RequestLinkUnverifiable("event set is unbounded or mixed")
    spans = _spans(batches, trace_id)
    verified = []
    for event in events:
        span = spans.get(event.span_id)
        when = int(event.occurred_at.timestamp() * 1_000_000_000)
        if (span and span.service == event.service
                and span.start - CLOCK_TOLERANCE_NS <= when <= span.end + CLOCK_TOLERANCE_NS
                and trace_fact_status(batches, event) == "VERIFIED_SPAN_FACT"):
            verified.append(event)
    output = []
    for upstream in verified:
        if upstream.failure_code not in {"upstream_http_5xx", "dependency_http_5xx"}:
            continue
        candidates = []
        for downstream in verified:
            if upstream.service == downstream.service or upstream.resource != downstream.service:
                continue
            path = _path(spans, upstream.span_id, downstream.span_id)
            if not path or len(path) < 2:
                continue
            # One service boundary with a verified parent path to the downstream server.
            boundary = next((i for i, span in enumerate(path)
                             if span.service != upstream.service), None)
            if (boundary is None or path[0].kind != "SPAN_KIND_SERVER"
                    or path[boundary].kind != "SPAN_KIND_SERVER"
                    or any(s.service != downstream.service for s in path[boundary:])
                    or any(s.end > p.end + CLOCK_TOLERANCE_NS
                           or s.start < p.start - CLOCK_TOLERANCE_NS
                           for p, s in zip(path, path[1:], strict=False))
                    or not 500 <= (path[0].http_status or 0) <= 599
                    or not 500 <= (path[boundary].http_status or 0) <= 599
                    or downstream.occurred_at > upstream.occurred_at):
                continue
            candidates.append((downstream, path))
        # Multiple downstream failure events are ambiguous, even on one trace.
        if len(candidates) != 1:
            continue
        downstream, path = candidates[0]
        output.append(RequestFailureLink(trace_id=trace_id, upstream_event_id=upstream.id,
            downstream_event_id=downstream.id, upstream_service=upstream.service,
            downstream_service=downstream.service,
            downstream_failure_code=downstream.failure_code,
            downstream_resource=downstream.resource, span_path=[s.id for s in path],
            observed_at=upstream.occurred_at))
    return output


def trace_events(settings: Settings, *, tenant_id: UUID, trace_id: str
                 ) -> list[RequestFailureEventView]:
    if not TRACE.fullmatch(trace_id):
        raise RequestLinkUnverifiable("invalid trace identity")
    with session_for(settings.database_url) as session:
        rows = session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.trace_id == trace_id,
        ).order_by(RequestFailureEventRecord.occurred_at, RequestFailureEventRecord.id)
          .limit(MAX_EVENTS + 1)).all()
        if len(rows) > MAX_EVENTS:
            raise RequestLinkUnverifiable("trace event limit exceeded")
        return [_view(row, "SOURCE_ASSERTED") for row in rows]


def recent_link_candidates(settings: Settings, *, tenant_id: UUID, service: str,
                           occurred_at: datetime) -> list[RequestFailureEventView]:
    from datetime import timedelta

    with session_for(settings.database_url) as session:
        rows = session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.service_name == service,
            RequestFailureEventRecord.occurred_at >= occurred_at - timedelta(seconds=30),
            RequestFailureEventRecord.occurred_at <= occurred_at + timedelta(seconds=30),
        ).order_by(RequestFailureEventRecord.occurred_at.desc(), RequestFailureEventRecord.id)
          .limit(MAX_CANDIDATES)).all()
        return [_view(row, "SOURCE_ASSERTED") for row in rows]
