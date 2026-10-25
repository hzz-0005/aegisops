"""Request cohorts use explicit event membership and never claim an alert binding."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aegisops.application.request_events import RequestFailureEventView
from aegisops.application.request_failure_cohorts import group_request_failures


def event(
    *, service: str = "order-service", code: str = "dependency_transport",
    resource: str = "payment-service", trace: str = "a" * 32, seconds: int = 0,
) -> RequestFailureEventView:
    return RequestFailureEventView(
        id=uuid4(), source_event_id=uuid4(), service=service,
        trace_id=trace, span_id="b" * 16, failure_code=code,
        resource=resource, occurred_at=datetime.now(UTC) + timedelta(seconds=seconds),
        provenance_status="SOURCE_ASSERTED",
    )


def test_repeated_events_on_one_trace_do_not_make_a_multi_request_cohort() -> None:
    events = [event(), event(seconds=1)]
    assert group_request_failures(events, min_distinct_traces=2) == []


def test_exact_membership_stays_separate_across_service_and_failure() -> None:
    first, second = event(), event(trace="c" * 32, seconds=1)
    unrelated_service = event(service="lab-gateway", trace="d" * 32)
    unrelated_failure = event(code="business_contract_rejected", trace="e" * 32)
    cohorts = group_request_failures(
        [first, second, unrelated_service, unrelated_failure], min_distinct_traces=2
    )
    assert len(cohorts) == 1
    cohort = cohorts[0]
    assert {member.id for member in cohort.members} == {first.id, second.id}
    assert cohort.distinct_trace_count == 2
    assert cohort.membership_status == "PERSISTED_EVENT_SET"
    assert cohort.alert_binding_status == "UNBOUND"
