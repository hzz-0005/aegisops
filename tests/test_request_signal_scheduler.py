"""Detector windows and retries have bounded, visible state."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import aegisops.application.request_signal_scheduler as scheduler
from aegisops.application.request_derived_signals import SignalWindowTooLarge
from aegisops.application.request_signal_scheduler import (
    ClaimedRun,
    finish_run,
    latest_eligible_window,
    retry_delay,
)
from aegisops.config import Settings
from aegisops.infrastructure.database.models import RequestSignalDetectorRunRecord


def test_scheduler_waits_for_complete_window_and_watermark() -> None:
    assert latest_eligible_window(datetime(2026, 10, 1, 12, 11, 59, tzinfo=UTC)) == datetime(
        2026, 10, 1, 11, 50, tzinfo=UTC
    )
    assert latest_eligible_window(datetime(2026, 10, 1, 12, 12, tzinfo=UTC)) == datetime(
        2026, 10, 1, 12, 0, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="timezone"):
        latest_eligible_window(datetime(2026, 10, 1, 12, 12))


def test_retry_delay_is_bounded() -> None:
    assert retry_delay(1) == timedelta(seconds=15)
    assert retry_delay(3) == timedelta(seconds=60)
    assert retry_delay(10) == timedelta(seconds=240)


@pytest.mark.parametrize(
    ("error", "attempt", "expected"),
    [
        (RuntimeError("temporary"), 1, "RETRYABLE"),
        (SignalWindowTooLarge(), 1, "BLOCKED"),
        (RuntimeError("temporary"), 5, "BLOCKED"),
    ],
)
def test_detector_failure_state_is_persisted(
    monkeypatch: pytest.MonkeyPatch, error: Exception, attempt: int, expected: str
) -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    run_id = uuid4()
    row = RequestSignalDetectorRunRecord(
        id=run_id, status="RUNNING", attempt_count=attempt, version=1,
        detector_version=scheduler.DETECTOR_VERSION,
        lease_expires_at=now + timedelta(minutes=10),
        window_start=now - timedelta(minutes=20),
    )
    session = MagicMock()
    session.scalar.return_value = row
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(scheduler, "session_for", lambda _url: context)
    claim = ClaimedRun(run_id, row.window_start, attempt)
    assert finish_run(Settings(), claim, error=error, now=now)
    assert row.status == expected
    assert row.last_error_code == type(error).__name__
    assert row.lease_expires_at is None
    assert (row.next_retry_at is not None) == (expected == "RETRYABLE")
