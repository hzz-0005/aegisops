"""Bounded, restartable scheduling for request symptom window detection."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from aegisops.application.request_derived_signals import (
    DETECTOR_VERSION,
    LATE_VERSION_PREFIX,
    MAX_WINDOW_EVENTS,
    WATERMARK_MINUTES,
    WINDOW_MINUTES,
    SignalMembershipChanged,
    SignalWindowTooLarge,
    _materialize_window,
    get_request_signal_window,
    materialize_closed_window,
    snapshot_digest,
)
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    RequestFailureEventRecord,
    RequestSignalDetectorRunRecord,
    RequestSignalWindowRecord,
)

MAX_ENQUEUE_PER_TICK = 6
MAX_ATTEMPTS = 5
LEASE_MINUTES = 10


class DetectorRunView(BaseModel):
    id: UUID
    detector_version: str
    window_start: datetime
    window_end: datetime
    status: Literal["PENDING", "RUNNING", "RETRYABLE", "BLOCKED", "FINALIZED"]
    attempt_count: int
    last_error_code: str | None
    next_retry_at: datetime | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    generation: int = 1
    detection_mode: Literal["ORIGINAL", "LATE_COMPENSATION"] = "ORIGINAL"
    source_window_id: UUID | None = None
    snapshot_event_count: int | None = None
    snapshot_complete: bool = True


@dataclass(frozen=True)
class ClaimedRun:
    id: UUID
    window_start: datetime
    attempt_count: int
    generation: int = 1
    detector_version: str = DETECTOR_VERSION
    source_window_id: UUID | None = None
    snapshot_event_ids: tuple[UUID, ...] | None = None


@dataclass(frozen=True)
class DetectorTick:
    window_start: datetime
    attempt_count: int
    status: Literal["FINALIZED", "RETRYABLE", "BLOCKED", "STALE"]
    error_code: str | None
    detection_mode: str = "ORIGINAL"


def validate_compensation_snapshot(
    source: RequestSignalWindowRecord, claim: ClaimedRun, tenant_id: UUID,
) -> None:
    ids = claim.snapshot_event_ids
    original_ids = [UUID(value) for value in source.snapshot_event_ids]
    if (source.tenant_id != tenant_id or source.id != claim.source_window_id
            or source.detector_version != DETECTOR_VERSION
            or source.window_start != claim.window_start
            or source.window_end != claim.window_start + timedelta(minutes=WINDOW_MINUTES)
            or source.event_count != len(original_ids)
            or len(set(original_ids)) != len(original_ids)
            or source.snapshot_digest != snapshot_digest(original_ids)
            or ids is None or len(set(ids)) != len(ids)
            or len(ids) > MAX_WINDOW_EVENTS or not set(original_ids) < set(ids)
            or claim.detector_version != LATE_VERSION_PREFIX + snapshot_digest(ids)):
        raise SignalMembershipChanged("late snapshot source or membership mismatch")


def enqueue_late_windows(settings: Settings) -> int:
    """Find previously unaccounted events without a fixed lookback cutoff."""
    require_dev_runtime(settings)
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        candidates = session.execute(text("""
            SELECT DISTINCT w.window_start, w.id FROM request_signal_windows w
            JOIN request_failure_events e ON e.tenant_id = w.tenant_id
                AND e.occurred_at >= w.window_start AND e.occurred_at < w.window_end
            WHERE w.tenant_id = :tenant AND w.detector_version = :original
              AND NOT (w.snapshot_event_ids ? e.id::text)
              AND NOT EXISTS (SELECT 1 FROM request_signal_detector_runs blocked
                WHERE blocked.tenant_id = w.tenant_id AND blocked.source_window_id = w.id
                  AND blocked.status = 'BLOCKED')
              AND NOT EXISTS (SELECT 1 FROM request_signal_detector_runs r
                WHERE r.tenant_id = w.tenant_id AND r.source_window_id = w.id
                  AND r.snapshot_event_ids ? e.id::text)
            ORDER BY w.window_start, w.id LIMIT :limit
        """), {"tenant": tenant_id, "original": DETECTOR_VERSION,
                 "limit": MAX_ENQUEUE_PER_TICK}).all()
        count = 0
        for start, source_id in candidates:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                            {"key": f"late-enqueue:{tenant_id}:{source_id}"})
            source = session.scalar(select(RequestSignalWindowRecord).where(
                RequestSignalWindowRecord.tenant_id == tenant_id,
                RequestSignalWindowRecord.id == source_id,
            ))
            if source is None:
                continue
            ids = list(session.scalars(select(RequestFailureEventRecord.id).where(
                RequestFailureEventRecord.tenant_id == tenant_id,
                RequestFailureEventRecord.occurred_at >= start,
                RequestFailureEventRecord.occurred_at < source.window_end,
            ).order_by(RequestFailureEventRecord.id).limit(MAX_WINDOW_EVENTS + 1)).all())
            version = LATE_VERSION_PREFIX + snapshot_digest(ids)
            # Oversized snapshots are stored as BLOCKED and never silently truncated into signals.
            oversized = len(ids) > MAX_WINDOW_EVENTS
            inserted = session.execute(insert(RequestSignalDetectorRunRecord).values(
                id=uuid4(), tenant_id=tenant_id, detector_version=version,
                window_start=start, window_end=source.window_end,
                source_window_id=source_id, snapshot_event_ids=[str(value) for value in ids],
                snapshot_complete=not oversized,
                status="BLOCKED" if oversized else "PENDING", attempt_count=0,
                last_error_code="SignalWindowTooLarge" if oversized else None,
            ).on_conflict_do_nothing(constraint="uq_request_signal_detector_run")
              .returning(RequestSignalDetectorRunRecord.id)).scalar_one_or_none()
            count += inserted is not None
        return count


def latest_eligible_window(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler time needs a timezone")
    ready = now.astimezone(UTC) - timedelta(minutes=WINDOW_MINUTES + WATERMARK_MINUTES)
    return ready.replace(
        minute=(ready.minute // WINDOW_MINUTES) * WINDOW_MINUTES,
        second=0, microsecond=0,
    )


def retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(15 * 2 ** (attempt_count - 1), 240))


def enqueue_due_windows(settings: Settings, *, now: datetime | None = None) -> int:
    require_dev_runtime(settings)
    current = now or datetime.now(UTC)
    latest = latest_eligible_window(current)
    tenant_id = settings.dev_tenant_id
    with session_for(settings.database_url) as session, session.begin():
        last = session.scalar(select(func.max(RequestSignalDetectorRunRecord.window_start)).where(
            RequestSignalDetectorRunRecord.tenant_id == tenant_id,
            RequestSignalDetectorRunRecord.detector_version == DETECTOR_VERSION,
        ))
        start = (
            last + timedelta(minutes=WINDOW_MINUTES)
            if last is not None
            else latest - timedelta(minutes=WINDOW_MINUTES * (MAX_ENQUEUE_PER_TICK - 1))
        )
        count = 0
        while start <= latest and count < MAX_ENQUEUE_PER_TICK:
            session.execute(insert(RequestSignalDetectorRunRecord).values(
                id=uuid4(), tenant_id=tenant_id, detector_version=DETECTOR_VERSION,
                window_start=start, window_end=start + timedelta(minutes=WINDOW_MINUTES),
                status="PENDING", attempt_count=0,
            ).on_conflict_do_nothing(constraint="uq_request_signal_detector_run"))
            count += 1
            start += timedelta(minutes=WINDOW_MINUTES)
        return count


def claim_due_run(settings: Settings, *, now: datetime | None = None,
                  compensation: bool = False) -> ClaimedRun | None:
    current = now or datetime.now(UTC)
    version_filter = (
        RequestSignalDetectorRunRecord.detector_version.startswith(LATE_VERSION_PREFIX)
        if compensation else RequestSignalDetectorRunRecord.detector_version == DETECTOR_VERSION
    )
    with session_for(settings.database_url) as session, session.begin():
        session.execute(update(RequestSignalDetectorRunRecord).where(
            RequestSignalDetectorRunRecord.tenant_id == settings.dev_tenant_id,
            version_filter,
            RequestSignalDetectorRunRecord.status == "RUNNING",
            RequestSignalDetectorRunRecord.lease_expires_at <= current,
            RequestSignalDetectorRunRecord.attempt_count >= MAX_ATTEMPTS,
        ).values(
            status="BLOCKED", last_error_code="LEASE_EXPIRED",
            lease_expires_at=None,
        ))
        row = session.scalar(
            select(RequestSignalDetectorRunRecord).where(
                RequestSignalDetectorRunRecord.tenant_id == settings.dev_tenant_id,
                version_filter,
                RequestSignalDetectorRunRecord.attempt_count < MAX_ATTEMPTS,
                RequestSignalDetectorRunRecord.snapshot_complete.is_(True),
                or_(
                    RequestSignalDetectorRunRecord.status == "PENDING",
                    and_(
                        RequestSignalDetectorRunRecord.status == "RETRYABLE",
                        RequestSignalDetectorRunRecord.next_retry_at <= current,
                    ),
                    and_(
                        RequestSignalDetectorRunRecord.status == "RUNNING",
                        RequestSignalDetectorRunRecord.lease_expires_at <= current,
                    ),
                ),
            ).order_by(RequestSignalDetectorRunRecord.window_start)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status = "RUNNING"
        row.attempt_count += 1
        row.started_at = current
        row.lease_expires_at = current + timedelta(minutes=LEASE_MINUTES)
        row.next_retry_at = None
        return ClaimedRun(row.id, row.window_start, row.attempt_count, row.version,
                          row.detector_version, row.source_window_id,
                          tuple(UUID(value) for value in row.snapshot_event_ids)
                          if row.snapshot_event_ids is not None else None)


def finish_run(
    settings: Settings, claim: ClaimedRun, *, error: Exception | None = None,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(UTC)
    with session_for(settings.database_url) as session, session.begin():
        row = session.scalar(select(RequestSignalDetectorRunRecord).where(
            RequestSignalDetectorRunRecord.tenant_id == settings.dev_tenant_id,
            RequestSignalDetectorRunRecord.id == claim.id,
        ).with_for_update())
        if (row is None or row.status != "RUNNING" or row.attempt_count != claim.attempt_count
                or row.version != claim.generation or row.detector_version != claim.detector_version
                or row.source_window_id != claim.source_window_id
                or row.lease_expires_at is None or row.lease_expires_at <= current
                or (tuple(UUID(value) for value in row.snapshot_event_ids)
                    if row.snapshot_event_ids is not None else None) != claim.snapshot_event_ids):
            return False
        row.lease_expires_at = None
        if error is None:
            row.status = "FINALIZED"
            row.completed_at = current
            row.last_error_code = None
            row.next_retry_at = None
        else:
            row.last_error_code = type(error).__name__
            if isinstance(error, SignalWindowTooLarge) or row.attempt_count >= MAX_ATTEMPTS:
                row.status = "BLOCKED"
                row.next_retry_at = None
            else:
                row.status = "RETRYABLE"
                row.next_retry_at = current + retry_delay(row.attempt_count)
        return True


def requeue_blocked_run(
    settings: Settings, run_id: UUID, *, expected_generation: int, operator_id: str, reason: str,
) -> dict:
    require_dev_runtime(settings)
    if not operator_id.strip() or not 3 <= len(reason.strip()) <= 300:
        raise ValueError("operator and a meaningful bounded reason are required")
    with session_for(settings.database_url) as session, session.begin():
        row = session.scalar(select(RequestSignalDetectorRunRecord).where(
            RequestSignalDetectorRunRecord.tenant_id == settings.dev_tenant_id,
            RequestSignalDetectorRunRecord.id == run_id,
        ).with_for_update())
        if row is None or row.status != "BLOCKED" or row.version != expected_generation:
            raise ValueError("run is not blocked at the expected generation")
        if row.snapshot_complete is False:
            raise ValueError("incomplete oversized snapshot cannot be requeued")
        operation = {
            "operator_id": operator_id.strip(), "reason": reason.strip(),
            "occurred_at": datetime.now(UTC).isoformat(), "old_status": row.status,
            "old_attempt_count": row.attempt_count, "old_error_code": row.last_error_code,
            "old_generation": row.version, "new_generation": row.version + 1,
        }
        row.requeue_history = [*(row.requeue_history or []), operation]
        row.version += 1
        row.status = "PENDING"
        row.attempt_count = 0
        row.last_error_code = None
        row.next_retry_at = row.lease_expires_at = row.started_at = row.completed_at = None
        return operation


def run_one_due_window(settings: Settings, *, compensation: bool = False) -> DetectorTick | None:
    if compensation:
        enqueue_late_windows(settings)
    else:
        enqueue_due_windows(settings)
    claim = claim_due_run(settings, compensation=compensation)
    if claim is None:
        return None
    try:
        if compensation:
            with session_for(settings.database_url) as session:
                source = session.scalar(select(RequestSignalWindowRecord).where(
                    RequestSignalWindowRecord.tenant_id == settings.dev_tenant_id,
                    RequestSignalWindowRecord.id == claim.source_window_id,
                ))
                if source is None:
                    raise SignalMembershipChanged("original window missing")
                validate_compensation_snapshot(source, claim, settings.dev_tenant_id)
            _materialize_window(settings, claim.window_start, version=claim.detector_version,
                                pinned_ids=claim.snapshot_event_ids)
        else:
            materialize_closed_window(settings, claim.window_start)
        if get_request_signal_window(settings, claim.window_start,
                                      version=claim.detector_version) is None:
            raise RuntimeError("detector did not finalize the window")
    except Exception as error:
        updated = finish_run(settings, claim, error=error)
        status: Literal["FINALIZED", "RETRYABLE", "BLOCKED", "STALE"] = (
            "STALE" if not updated else
            "BLOCKED" if isinstance(error, SignalWindowTooLarge)
            or claim.attempt_count >= MAX_ATTEMPTS else "RETRYABLE"
        )
        return DetectorTick(claim.window_start, claim.attempt_count, status, type(error).__name__,
                            "LATE_COMPENSATION" if compensation else "ORIGINAL")
    else:
        updated = finish_run(settings, claim)
        return DetectorTick(
            claim.window_start, claim.attempt_count,
            "FINALIZED" if updated else "STALE", None,
            "LATE_COMPENSATION" if compensation else "ORIGINAL",
        )


def list_detector_runs(settings: Settings, *, limit: int = 100,
                       compensation: bool = False) -> list[DetectorRunView]:
    with session_for(settings.database_url) as session:
        rows = session.scalars(select(RequestSignalDetectorRunRecord).where(
            RequestSignalDetectorRunRecord.tenant_id == settings.dev_tenant_id,
            (RequestSignalDetectorRunRecord.detector_version.startswith(LATE_VERSION_PREFIX)
             if compensation else
             RequestSignalDetectorRunRecord.detector_version == DETECTOR_VERSION),
        ).order_by(RequestSignalDetectorRunRecord.created_at.desc(),
                   RequestSignalDetectorRunRecord.id).limit(limit)).all()
        return [DetectorRunView(
            id=row.id, detector_version=row.detector_version,
            window_start=row.window_start, window_end=row.window_end,
            status=cast(Literal[
                "PENDING", "RUNNING", "RETRYABLE", "BLOCKED", "FINALIZED"
            ], row.status),
            attempt_count=row.attempt_count, last_error_code=row.last_error_code,
            next_retry_at=row.next_retry_at, lease_expires_at=row.lease_expires_at,
            started_at=row.started_at, completed_at=row.completed_at,
            generation=row.version,
            detection_mode="LATE_COMPENSATION" if row.source_window_id else "ORIGINAL",
            source_window_id=row.source_window_id,
            snapshot_event_count=(len(row.snapshot_event_ids)
                                  if row.snapshot_event_ids is not None else None),
            snapshot_complete=row.snapshot_complete,
        ) for row in rows]
