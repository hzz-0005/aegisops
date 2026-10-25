"""Late arrivals create new provenance; original windows remain immutable."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from aegisops.application.request_derived_signals import (
    DETECTOR_VERSION,
    LATE_VERSION_PREFIX,
    SignalMembershipChanged,
    _materialize_window,
    _view,
    snapshot_digest,
)
from aegisops.application.request_signal_scheduler import (
    ClaimedRun,
    finish_run,
    requeue_blocked_run,
    validate_compensation_snapshot,
)
from aegisops.config import Settings
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalRecord,
    RequestSignalDetectorRunRecord,
    RequestSignalWindowRecord,
)


def snapshot():
    original, late = uuid4(), uuid4()
    tenant, source_id = uuid4(), uuid4()
    start = datetime(2026, 10, 8, 5, 20, tzinfo=UTC)
    source = RequestSignalWindowRecord(
        id=source_id,
        tenant_id=tenant,
        detector_version=DETECTOR_VERSION,
        window_start=start,
        window_end=start + timedelta(minutes=10),
        event_count=1,
        signal_count=0,
        snapshot_event_ids=[str(original)],
        snapshot_digest=snapshot_digest([original]),
    )
    ids = (original, late)
    claim = ClaimedRun(
        uuid4(), start, 1, 1, LATE_VERSION_PREFIX + snapshot_digest(ids), source_id, ids
    )
    return source, claim, tenant


def test_compensation_combines_original_and_late_without_mutating_original():
    source, claim, tenant = snapshot()
    validate_compensation_snapshot(source, claim, tenant)
    assert source.event_count == 1 and len(source.snapshot_event_ids) == 1
    assert len(claim.snapshot_event_ids) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "tenant",
        "source",
        "version",
        "digest",
        "time",
        "duplicate",
        "missing_original",
        "no_late",
        "count",
    ],
)
def test_corrupt_cross_tenant_or_non_late_snapshot_is_rejected(mutation):
    source, claim, tenant = snapshot()
    if mutation == "tenant":
        tenant = uuid4()
    elif mutation == "source":
        source.id = uuid4()
    elif mutation == "version":
        source.detector_version = "late-v1:wrong"
    elif mutation == "digest":
        source.snapshot_digest = "wrong"
    elif mutation == "time":
        source.window_start += timedelta(minutes=10)
    elif mutation == "count":
        source.event_count = 2
    else:
        ids = claim.snapshot_event_ids
        assert ids is not None
        replacement = (
            (ids[1], ids[1])
            if mutation == "duplicate"
            else ((ids[1],) if mutation == "missing_original" else (ids[0],))
        )
        claim = ClaimedRun(
            claim.id,
            claim.window_start,
            1,
            1,
            LATE_VERSION_PREFIX + snapshot_digest(replacement),
            claim.source_window_id,
            replacement,
        )
    with pytest.raises(SignalMembershipChanged):
        validate_compensation_snapshot(source, claim, tenant)


def test_snapshot_version_cannot_be_reused_for_different_members():
    source, claim, tenant = snapshot()
    wrong = ClaimedRun(
        claim.id,
        claim.window_start,
        1,
        1,
        claim.detector_version,
        source.id,
        (*claim.snapshot_event_ids, uuid4()),
    )
    with pytest.raises(SignalMembershipChanged):
        validate_compensation_snapshot(source, wrong, tenant)


def test_materialization_rejects_unpinned_or_forged_version_before_database():
    start = datetime(2026, 10, 8, 5, 20, tzinfo=UTC)
    with pytest.raises(SignalMembershipChanged):
        _materialize_window(Settings(), start, version="late-v1:forged")
    with pytest.raises(SignalMembershipChanged):
        _materialize_window(Settings(), start, version="late-v1:forged", pinned_ids=[uuid4()])


def test_missing_member_aborts_before_verification_and_write(monkeypatch):
    import aegisops.application.request_derived_signals as module

    source, claim, _tenant = snapshot()
    session = MagicMock()
    session.scalar.return_value = None
    session.scalars.return_value.all.return_value = []
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(module, "session_for", lambda _url: context)
    verify = MagicMock()
    monkeypatch.setattr(module, "verify_request_event", verify)
    with pytest.raises(SignalMembershipChanged, match="missing"):
        _materialize_window(
            Settings(),
            source.window_start,
            version=claim.detector_version,
            pinned_ids=claim.snapshot_event_ids,
        )
    verify.assert_not_called()
    session.add.assert_not_called()


def test_late_signal_is_still_only_an_unbound_symptom():
    now = datetime.now(UTC)
    signal = RequestDerivedSignalRecord(
        id=uuid4(),
        detector_version="late-v1:abc",
        service_name="orders",
        failure_code="dependency_timeout",
        resource_name="payments",
        window_start=now,
        window_end=now + timedelta(minutes=10),
        event_count=2,
        distinct_trace_count=2,
        created_at=now,
    )
    view = _view(signal, [])
    assert view.detection_mode == "LATE_COMPENSATION"
    assert view.alert_binding_status == "UNBOUND"
    assert view.root_cause_status == "UNDETERMINED"


def mock_session(monkeypatch, row):
    import aegisops.application.request_signal_scheduler as module

    session = MagicMock()
    session.scalar.return_value = row
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(module, "session_for", lambda _url: context)
    return session


def test_incomplete_snapshot_cannot_be_requeued_as_complete(monkeypatch):
    row = RequestSignalDetectorRunRecord(
        id=uuid4(), status="BLOCKED", version=1, snapshot_complete=False
    )
    mock_session(monkeypatch, row)
    with pytest.raises(ValueError, match="incomplete"):
        requeue_blocked_run(
            Settings(), row.id, expected_generation=1, operator_id="operator", reason="审查超大窗口"
        )
    assert row.status == "BLOCKED" and row.version == 1


@pytest.mark.parametrize(
    "mutation", ["generation", "attempt", "version", "source", "ids", "expired", "missing_lease"]
)
def test_stale_or_changed_claim_cannot_finalize(monkeypatch, mutation):
    source, claim, _tenant = snapshot()
    now = datetime.now(UTC)
    row = RequestSignalDetectorRunRecord(
        id=claim.id,
        status="RUNNING",
        attempt_count=1,
        version=1,
        detector_version=claim.detector_version,
        source_window_id=source.id,
        snapshot_event_ids=[str(value) for value in claim.snapshot_event_ids],
        lease_expires_at=now + timedelta(minutes=10),
    )
    if mutation == "generation":
        row.version = 2
    elif mutation == "attempt":
        row.attempt_count = 2
    elif mutation == "version":
        row.detector_version = "other"
    elif mutation == "source":
        row.source_window_id = uuid4()
    elif mutation == "ids":
        row.snapshot_event_ids.append(str(uuid4()))
    elif mutation == "expired":
        row.lease_expires_at = now
    else:
        row.lease_expires_at = None
    mock_session(monkeypatch, row)
    assert finish_run(Settings(), claim, now=now) is False
    assert row.status == "RUNNING"
