"""Persist exact, verified request members for a closed detector window."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select, text, true
from sqlalchemy.orm import Session

from aegisops.application.request_events import verify_request_event
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalEventRecord,
    RequestDerivedSignalRecord,
    RequestFailureEventRecord,
    RequestSignalWindowRecord,
)

DETECTOR_VERSION = "request-failure-window-v1"
WINDOW_MINUTES = 10
MAX_WINDOW_EVENTS = 1000
MAX_SIGNAL_MEMBERS = 64
WATERMARK_MINUTES = 2
LATE_VERSION_PREFIX = "late-v1:"


class SignalMemberVerificationView(BaseModel):
    request_event_id: UUID
    verification_status: Literal["VERIFIED_SPAN_FACT"]
    verification_recorded_at: datetime | None


class RequestDerivedSignalView(BaseModel):
    id: UUID
    detector_version: str
    detection_mode: Literal["ORIGINAL", "LATE_COMPENSATION"] = "ORIGINAL"
    signal_kind: Literal["REQUEST_FAILURE_SYMPTOM"] = "REQUEST_FAILURE_SYMPTOM"
    service: str
    failure_code: str
    resource: str | None
    window_start: datetime
    window_end: datetime
    member_event_ids: list[UUID]
    member_verifications: list[SignalMemberVerificationView]
    event_count: int
    distinct_trace_count: int
    verification_status: Literal["VERIFIED_SPAN_FACT"] = "VERIFIED_SPAN_FACT"
    alert_binding_status: Literal["UNBOUND"] = "UNBOUND"
    root_cause_status: Literal["UNDETERMINED"] = "UNDETERMINED"
    created_at: datetime


class RequestSignalWindowView(BaseModel):
    detector_version: str
    window_start: datetime
    window_end: datetime
    finalized_at: datetime
    snapshot_event_ids: list[UUID]
    snapshot_digest: str
    event_count: int
    signal_count: int
    late_event_ids: list[UUID]
    late_event_count: int
    status: Literal["FINALIZED"] = "FINALIZED"


class SignalWindowInvalid(Exception):
    pass


class SignalWindowTooLarge(Exception):
    pass


class SignalMembershipChanged(Exception):
    pass


class SignalEventNotVerified(Exception):
    pass


def validate_closed_window(start: datetime, *, now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    if start.tzinfo is None or start.utcoffset() is None:
        raise SignalWindowInvalid("window start needs a timezone")
    start = start.astimezone(UTC)
    if start.minute % WINDOW_MINUTES or start.second or start.microsecond:
        raise SignalWindowInvalid("window start must align to a 10 minute boundary")
    end = start + timedelta(minutes=WINDOW_MINUTES)
    if end > current - timedelta(minutes=WATERMARK_MINUTES):
        raise SignalWindowInvalid("window watermark has not passed")
    return end


def signal_identity(service: str, failure_code: str, resource: str | None) -> str:
    value = json.dumps([service, failure_code, resource], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def snapshot_digest(event_ids: Sequence[UUID]) -> str:
    serialized = json.dumps(sorted(str(event_id) for event_id in event_ids), separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _view(
    record: RequestDerivedSignalRecord, members: Sequence[RequestDerivedSignalEventRecord]
) -> RequestDerivedSignalView:
    members = sorted(members, key=lambda member: member.request_event_id)
    return RequestDerivedSignalView(
        id=record.id,
        detector_version=record.detector_version,
        detection_mode=("LATE_COMPENSATION" if record.detector_version.startswith(
            LATE_VERSION_PREFIX) else "ORIGINAL"),
        service=record.service_name,
        failure_code=record.failure_code,
        resource=record.resource_name,
        window_start=record.window_start,
        window_end=record.window_end,
        member_event_ids=[member.request_event_id for member in members],
        member_verifications=[
            SignalMemberVerificationView(
                request_event_id=member.request_event_id,
                verification_status=cast(
                    Literal["VERIFIED_SPAN_FACT"], member.verification_status
                ),
                verification_recorded_at=member.verification_recorded_at,
            )
            for member in members
        ],
        event_count=record.event_count,
        distinct_trace_count=record.distinct_trace_count,
        created_at=record.created_at,
    )


def _window_signals(
    session: Session, tenant_id: UUID, start: datetime, version: str = DETECTOR_VERSION,
) -> list[RequestDerivedSignalView]:
    records = session.scalars(
        select(RequestDerivedSignalRecord).where(
            RequestDerivedSignalRecord.tenant_id == tenant_id,
            RequestDerivedSignalRecord.detector_version == version,
            RequestDerivedSignalRecord.window_start == start,
        ).order_by(RequestDerivedSignalRecord.id)
    ).all()
    return [
        _view(record, session.scalars(
            select(RequestDerivedSignalEventRecord).where(
                RequestDerivedSignalEventRecord.tenant_id == tenant_id,
                RequestDerivedSignalEventRecord.signal_id == record.id,
            )
        ).all())
        for record in records
    ]


def materialize_closed_window(
    settings: Settings, start: datetime
) -> list[RequestDerivedSignalView]:
    return _materialize_window(settings, start)


def _materialize_window(
    settings: Settings, start: datetime, *, version: str = DETECTOR_VERSION,
    pinned_ids: Sequence[UUID] | None = None,
) -> list[RequestDerivedSignalView]:
    """Original windows read current rows; late revisions read only their pinned snapshot."""
    if pinned_ids is not None:
        if (len(set(pinned_ids)) != len(pinned_ids) or len(pinned_ids) > MAX_WINDOW_EVENTS
                or version != LATE_VERSION_PREFIX + snapshot_digest(pinned_ids)):
            raise SignalMembershipChanged("invalid compensation snapshot identity")
    elif version != DETECTOR_VERSION:
        raise SignalMembershipChanged("compensation requires a pinned snapshot")
    end = validate_closed_window(start)
    start = start.astimezone(UTC)
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session:
        finalized = session.scalar(select(RequestSignalWindowRecord.id).where(
            RequestSignalWindowRecord.tenant_id == tenant_id,
            RequestSignalWindowRecord.detector_version == version,
            RequestSignalWindowRecord.window_start == start,
        ))
        if finalized is not None:
            return _window_signals(session, tenant_id, start, version)
        rows = session.scalars(
            select(RequestFailureEventRecord)
            .where(
                RequestFailureEventRecord.tenant_id == tenant_id,
                RequestFailureEventRecord.occurred_at >= start,
                RequestFailureEventRecord.occurred_at < end,
                (RequestFailureEventRecord.id.in_(pinned_ids)
                 if pinned_ids is not None else true()),
            )
            .order_by(RequestFailureEventRecord.occurred_at, RequestFailureEventRecord.id)
            .limit(MAX_WINDOW_EVENTS + 1)
        ).all()
        if len(rows) > MAX_WINDOW_EVENTS:
            raise SignalWindowTooLarge
        if pinned_ids is not None and set(row.id for row in rows) != set(pinned_ids):
            raise SignalMembershipChanged("compensation source members missing")
        existing_records = session.scalars(select(RequestDerivedSignalRecord).where(
            RequestDerivedSignalRecord.tenant_id == tenant_id,
            RequestDerivedSignalRecord.detector_version == version,
            RequestDerivedSignalRecord.window_start == start,
        )).all()
        existing_ids = {
            record.identity_key: sorted(session.scalars(
                select(RequestDerivedSignalEventRecord.request_event_id).where(
                    RequestDerivedSignalEventRecord.tenant_id == tenant_id,
                    RequestDerivedSignalEventRecord.signal_id == record.id,
                )
            ).all())
            for record in existing_records
        }

    groups: dict[tuple[str, str, str | None], list[RequestFailureEventRecord]] = defaultdict(list)
    for row in rows:
        groups[(row.service_name, row.failure_code, row.resource_name)].append(row)
    eligible: list[tuple[str, str, str | None, str, list[UUID], int]] = []
    verified_at: dict[UUID, datetime] = {}
    for (service, code, resource), members in sorted(groups.items(), key=lambda item: item[0][:2]):
        trace_count = len({member.trace_id for member in members})
        if trace_count < 2:
            continue
        if len(members) > MAX_SIGNAL_MEMBERS:
            raise SignalWindowTooLarge
        ids = sorted(member.id for member in members)
        identity = signal_identity(service, code, resource)
        if identity in existing_ids:
            if existing_ids[identity] != ids:
                raise SignalMembershipChanged
        else:
            for event_id in ids:
                verified = verify_request_event(settings, event_id)
                if verified is None or verified.provenance_status != "VERIFIED_SPAN_FACT":
                    raise SignalEventNotVerified(str(event_id))
                verified_at[event_id] = datetime.now(UTC)
        eligible.append((service, code, resource, identity, ids, trace_count))
    if set(existing_ids) - {item[3] for item in eligible}:
        raise SignalMembershipChanged

    snapshot_ids = sorted(row.id for row in rows)
    with session_for(settings.database_url) as session, session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{tenant_id}:{DETECTOR_VERSION}:{start.isoformat()}"},
        )
        finalized = session.scalar(select(RequestSignalWindowRecord.id).where(
            RequestSignalWindowRecord.tenant_id == tenant_id,
            RequestSignalWindowRecord.detector_version == version,
            RequestSignalWindowRecord.window_start == start,
        ))
        if finalized is not None:
            return _window_signals(session, tenant_id, start, version)
        current_ids = sorted(session.scalars(
            select(RequestFailureEventRecord.id).where(
                RequestFailureEventRecord.tenant_id == tenant_id,
                RequestFailureEventRecord.occurred_at >= start,
                RequestFailureEventRecord.occurred_at < end,
                (RequestFailureEventRecord.id.in_(pinned_ids)
                 if pinned_ids is not None else true()),
            )
        ).all())
        if current_ids != snapshot_ids:
            raise SignalMembershipChanged
        result: list[RequestDerivedSignalView] = []
        for service, code, resource, identity, ids, trace_count in eligible:
            if pinned_ids is not None:
                prior = session.scalars(select(RequestDerivedSignalRecord).where(
                    RequestDerivedSignalRecord.tenant_id == tenant_id,
                    RequestDerivedSignalRecord.window_start == start,
                    RequestDerivedSignalRecord.identity_key == identity,
                    RequestDerivedSignalRecord.detector_version != version,
                )).all()
                unchanged = any(sorted(session.scalars(
                    select(RequestDerivedSignalEventRecord.request_event_id).where(
                        RequestDerivedSignalEventRecord.tenant_id == tenant_id,
                        RequestDerivedSignalEventRecord.signal_id == previous.id,
                    )).all()) == ids for previous in prior)
                if unchanged:
                    continue  # New revision, identical symptom members: no duplicate signal.
            existing = session.scalar(select(RequestDerivedSignalRecord).where(
                RequestDerivedSignalRecord.tenant_id == tenant_id,
                RequestDerivedSignalRecord.detector_version == version,
                RequestDerivedSignalRecord.identity_key == identity,
                RequestDerivedSignalRecord.window_start == start,
            ))
            if existing is not None:
                stored_members = session.scalars(select(RequestDerivedSignalEventRecord).where(
                    RequestDerivedSignalEventRecord.tenant_id == tenant_id,
                    RequestDerivedSignalEventRecord.signal_id == existing.id,
                )).all()
                if sorted(member.request_event_id for member in stored_members) != ids:
                    raise SignalMembershipChanged
                result.append(_view(existing, stored_members))
                continue
            record = RequestDerivedSignalRecord(
                id=uuid4(), tenant_id=tenant_id, detector_version=version,
                identity_key=identity, service_name=service, failure_code=code,
                resource_name=resource, window_start=start, window_end=end,
                event_count=len(ids), distinct_trace_count=trace_count,
                verification_status="VERIFIED_SPAN_FACT",
            )
            session.add(record)
            session.flush()
            new_members = [
                RequestDerivedSignalEventRecord(
                    id=uuid4(), tenant_id=tenant_id, signal_id=record.id,
                    request_event_id=event_id, verification_status="VERIFIED_SPAN_FACT",
                    verification_recorded_at=verified_at[event_id],
                )
                for event_id in ids
            ]
            session.add_all(new_members)
            session.refresh(record)
            result.append(_view(record, new_members))
        session.add(RequestSignalWindowRecord(
            id=uuid4(), tenant_id=tenant_id, detector_version=version,
            window_start=start, window_end=end,
            snapshot_event_ids=[str(event_id) for event_id in snapshot_ids],
            snapshot_digest=snapshot_digest(snapshot_ids),
            event_count=len(snapshot_ids), signal_count=len(result),
        ))
        return result


def get_request_signal_window(
    settings: Settings, start: datetime, *, version: str = DETECTOR_VERSION,
) -> RequestSignalWindowView | None:
    if start.tzinfo is None or start.utcoffset() is None:
        raise SignalWindowInvalid("window start needs a timezone")
    start = start.astimezone(UTC)
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session:
        record = session.scalar(select(RequestSignalWindowRecord).where(
            RequestSignalWindowRecord.tenant_id == tenant_id,
            RequestSignalWindowRecord.detector_version == version,
            RequestSignalWindowRecord.window_start == start,
        ))
        if record is None:
            return None
        current_ids = set(session.scalars(select(RequestFailureEventRecord.id).where(
            RequestFailureEventRecord.tenant_id == tenant_id,
            RequestFailureEventRecord.occurred_at >= record.window_start,
            RequestFailureEventRecord.occurred_at < record.window_end,
        )).all())
        snapshot_ids = [UUID(value) for value in record.snapshot_event_ids]
        late_ids = sorted(current_ids - set(snapshot_ids))
        return RequestSignalWindowView(
            detector_version=record.detector_version,
            window_start=record.window_start, window_end=record.window_end,
            finalized_at=record.finalized_at,
            snapshot_event_ids=snapshot_ids, snapshot_digest=record.snapshot_digest,
            event_count=record.event_count, signal_count=record.signal_count,
            late_event_ids=late_ids, late_event_count=len(late_ids),
        )


def list_request_signals(settings: Settings, *, limit: int = 100) -> list[RequestDerivedSignalView]:
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session:
        records = session.scalars(
            select(RequestDerivedSignalRecord)
            .where(RequestDerivedSignalRecord.tenant_id == tenant_id)
            .order_by(RequestDerivedSignalRecord.window_start.desc(),
                      RequestDerivedSignalRecord.created_at.desc(), RequestDerivedSignalRecord.id)
            .limit(limit)
        ).all()
        result = []
        for record in records:
            members = session.scalars(
                select(RequestDerivedSignalEventRecord).where(
                    RequestDerivedSignalEventRecord.tenant_id == tenant_id,
                    RequestDerivedSignalEventRecord.signal_id == record.id,
                ).order_by(RequestDerivedSignalEventRecord.request_event_id)
            ).all()
            result.append(_view(record, members))
        return result
