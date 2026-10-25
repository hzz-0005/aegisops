"""Source membership cannot be fabricated by a nearby or mislabeled alert."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegisops.application import request_signal_alerts as module
from aegisops.application.request_derived_signals import signal_identity
from aegisops.application.request_signal_alerts import SignalPromotionInput, validate_members
from aegisops.config import Settings
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalEventRecord as Member,
)
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalRecord as Signal,
)
from aegisops.infrastructure.database.models import (
    RequestFailureEventRecord as Event,
)


def sample():
    tenant, signal_id = uuid4(), uuid4()
    start = datetime(2026, 10, 8, 10, 10, tzinfo=UTC)
    signal = Signal(id=signal_id, tenant_id=tenant, service_name="orders",
        failure_code="dependency_transport", resource_name="payments",
        identity_key=signal_identity("orders", "dependency_transport", "payments"),
        window_start=start, window_end=start + timedelta(minutes=10),
        event_count=2, distinct_trace_count=2, verification_status="VERIFIED_SPAN_FACT")
    events = [Event(id=uuid4(), tenant_id=tenant, service_name="orders",
        failure_code=signal.failure_code, resource_name="payments", trace_id=f"{i:032x}",
        occurred_at=start + timedelta(seconds=i)) for i in (1, 2)]
    members = [Member(tenant_id=tenant, signal_id=signal_id, request_event_id=event.id,
        verification_status="VERIFIED_SPAN_FACT", verification_recorded_at=start)
        for event in events]
    return signal, members, events


def test_exact_members_are_accepted():
    validate_members(*sample())


@pytest.mark.parametrize("mutation", ["tenant", "service", "resource", "code", "time",
                                     "duplicate", "missing", "unverified", "timestamp",
                                     "signal", "count", "identity", "trace"])
def test_membership_scope_and_integrity_fail_closed(mutation):
    signal, members, events = sample()
    if mutation in {"tenant", "service", "resource", "code"}:
        names = {"tenant": "tenant_id", "service": "service_name",
                 "resource": "resource_name", "code": "failure_code"}
        setattr(events[0], names[mutation], uuid4() if mutation == "tenant" else "foreign")
    elif mutation == "time":
        events[0].occurred_at = signal.window_end
    elif mutation == "duplicate":
        members[1] = deepcopy(members[0])
    elif mutation == "missing":
        events.pop()
    elif mutation == "unverified":
        members[0].verification_status = "SOURCE_ASSERTED"
    elif mutation == "timestamp":
        members[0].verification_recorded_at = None
    elif mutation == "signal":
        members[0].signal_id = uuid4()
    elif mutation == "count":
        signal.event_count = 3
    elif mutation == "identity":
        signal.identity_key = "foreign"
    elif mutation == "trace":
        events[0].trace_id = events[1].trace_id
    with pytest.raises(ValueError):
        validate_members(signal, members, events)


def test_promotion_requires_reason_and_rejects_client_supplied_members():
    with pytest.raises(ValidationError):
        SignalPromotionInput(reason="   ")
    with pytest.raises(ValidationError):
        SignalPromotionInput(reason="核对来源成员", member_event_ids=[str(uuid4())])


def test_prometheus_cannot_claim_direct_membership_by_copying_labels(monkeypatch):
    session = MagicMock()
    session.scalar.return_value.source = "prometheus"
    session.scalar.return_value.payload = {"request_signal_id": str(uuid4())}
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(module, "session_for", lambda _: context)
    assert module.membership_evidence(Settings(), tenant_id=uuid4(), alert_id=uuid4()) == []
    assert session.scalar.call_count == 1
