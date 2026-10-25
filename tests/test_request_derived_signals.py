"""Detector windows are fixed; a symptom identity never implies a root cause."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import aegisops.application.request_derived_signals as detector
from aegisops.application.request_derived_signals import (
    SignalWindowInvalid,
    _view,
    get_request_signal_window,
    signal_identity,
    snapshot_digest,
    validate_closed_window,
)
from aegisops.config import Settings
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalEventRecord,
    RequestDerivedSignalRecord,
    RequestSignalWindowRecord,
)


def test_closed_detector_window_is_utc_aligned() -> None:
    start = datetime(2026, 9, 30, 8, 50, tzinfo=UTC)
    assert validate_closed_window(start, now=start + timedelta(minutes=12)) == start + timedelta(
        minutes=10
    )


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 9, 30, 8, 51, tzinfo=UTC),
        datetime(2026, 9, 30, 8, 50),
    ],
)
def test_detector_rejects_misaligned_or_naive_window(start: datetime) -> None:
    with pytest.raises(SignalWindowInvalid):
        validate_closed_window(start)


def test_detector_rejects_open_window() -> None:
    start = datetime(2026, 9, 30, 8, 50, tzinfo=UTC)
    with pytest.raises(SignalWindowInvalid, match="watermark"):
        validate_closed_window(start, now=start + timedelta(minutes=10, seconds=20))


def test_detector_rejects_window_before_two_minute_watermark() -> None:
    start = datetime(2026, 9, 30, 8, 50, tzinfo=UTC)
    with pytest.raises(SignalWindowInvalid, match="watermark"):
        validate_closed_window(start, now=start + timedelta(minutes=11, seconds=59))


def test_window_snapshot_digest_is_order_independent() -> None:
    first, second = uuid4(), uuid4()
    assert snapshot_digest([first, second]) == snapshot_digest([second, first])
    assert snapshot_digest([first]) != snapshot_digest([first, second])


def test_finalized_window_reports_late_ids_without_changing_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 9, 30, 8, 50, tzinfo=UTC)
    first, second, late = uuid4(), uuid4(), uuid4()
    record = RequestSignalWindowRecord(
        detector_version="request-failure-window-v1",
        window_start=start, window_end=start + timedelta(minutes=10),
        finalized_at=start + timedelta(minutes=12),
        snapshot_event_ids=[str(first), str(second)],
        snapshot_digest=snapshot_digest([first, second]),
        event_count=2, signal_count=1,
    )
    session = MagicMock()
    session.scalar.return_value = record
    session.scalars.return_value.all.return_value = [first, second, late]
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(detector, "session_for", lambda _url: context)
    view = get_request_signal_window(Settings(), start)
    assert view is not None
    assert set(view.snapshot_event_ids) == {first, second}
    assert view.late_event_ids == [late]
    assert view.late_event_count == 1
    assert view.event_count == 2


def test_identity_keeps_resource_and_failure_separate() -> None:
    key = signal_identity("order-service", "dependency_transport", "payment-service")
    assert key == signal_identity("order-service", "dependency_transport", "payment-service")
    assert key != signal_identity("order-service", "dependency_transport", "inventory-service")
    assert key != signal_identity("order-service", "business_contract_rejected", "payment-service")


def test_signal_view_keeps_each_members_verification_result() -> None:
    now = datetime(2026, 9, 30, 9, tzinfo=UTC)
    signal_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    record = RequestDerivedSignalRecord(
        id=signal_id, detector_version="request-failure-window-v1",
        service_name="order-service", failure_code="dependency_transport",
        resource_name="payment-service", window_start=now,
        window_end=now + timedelta(minutes=10), event_count=2,
        distinct_trace_count=2, created_at=now,
    )
    members = [
        RequestDerivedSignalEventRecord(
            request_event_id=first_id, verification_status="VERIFIED_SPAN_FACT",
            verification_recorded_at=now,
        ),
        RequestDerivedSignalEventRecord(
            request_event_id=second_id, verification_status="VERIFIED_SPAN_FACT",
            verification_recorded_at=None,
        ),
    ]
    view = _view(record, members)
    assert view.signal_kind == "REQUEST_FAILURE_SYMPTOM"
    assert view.root_cause_status == "UNDETERMINED"
    assert view.alert_binding_status == "UNBOUND"
    assert set(view.member_event_ids) == {first_id, second_id}
    assert {item.request_event_id for item in view.member_verifications} == {
        first_id, second_id,
    }
    assert all(
        item.verification_status == "VERIFIED_SPAN_FACT"
        for item in view.member_verifications
    )
    assert next(
        item for item in view.member_verifications if item.request_event_id == second_id
    ).verification_recorded_at is None
