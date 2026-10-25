"""A restart backlog drains in bounded batches; idle polling stops early."""

import sys
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

import apps.request_signal_detector.main as detector
from aegisops.application.request_signal_scheduler import DetectorTick
from aegisops.config import Settings


def test_backlog_batch_has_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    tick = DetectorTick(datetime.now(UTC), 1, "FINALIZED", None)
    run = Mock(return_value=tick)
    monkeypatch.setattr(detector, "run_one_due_window", run)
    settings = Settings()
    assert detector.poll_batch(settings) == detector.BATCH_SIZE * 2
    assert run.call_count == detector.BATCH_SIZE * 2
    run.assert_called_with(settings, compensation=True)


def test_batch_stops_when_no_window_is_due(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(side_effect=[DetectorTick(datetime.now(UTC), 1, "RETRYABLE", "Timeout"), None,
                           None])
    monkeypatch.setattr(detector, "run_one_due_window", run)
    assert detector.poll_batch(Settings()) == 1
    assert run.call_count == 3


def test_original_backlog_cannot_starve_compensation(monkeypatch: pytest.MonkeyPatch) -> None:
    tick = DetectorTick(datetime.now(UTC), 1, "FINALIZED", None)
    late_calls = []
    def run(_settings, *, compensation=False):
        if compensation:
            late_calls.append(True)
        return tick
    monkeypatch.setattr(detector, "run_one_due_window", run)
    detector.poll_batch(Settings())
    assert len(late_calls) == detector.BATCH_SIZE


@pytest.mark.parametrize("processed,delay", [(detector.BATCH_SIZE, 1), (0, 10)])
def test_polling_adapts_to_backlog(
    monkeypatch: pytest.MonkeyPatch, processed: int, delay: int,
) -> None:
    monkeypatch.setattr(sys, "argv", ["detector"])
    monkeypatch.setattr(detector, "poll_batch", lambda _settings: processed)
    sleep = Mock(side_effect=InterruptedError("end test loop"))
    monkeypatch.setattr(detector.time, "sleep", sleep)
    with pytest.raises(InterruptedError):
        detector.main()
    sleep.assert_called_once_with(delay)
