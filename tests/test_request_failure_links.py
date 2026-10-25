"""Same trace/time cannot replace verified ancestry, response order and unique membership."""

import base64
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegisops.application.request_events import RequestFailureEventView
from aegisops.application.request_failure_links import RequestLinkUnverifiable, links_for_events

TRACE = "a" * 32
START = 1_790_000_000


def binary(value):
    return base64.b64encode(bytes.fromhex(value)).decode()


def sample():
    upstream = RequestFailureEventView(id=uuid4(), source_event_id=uuid4(), service="gateway",
        trace_id=TRACE, span_id="1" * 16, failure_code="upstream_http_5xx", resource="orders",
        occurred_at=datetime.fromtimestamp(START + 3.8, UTC), provenance_status="SOURCE_ASSERTED")
    downstream = upstream.model_copy(update={"id": uuid4(), "source_event_id": uuid4(),
        "service": "orders", "span_id": "3" * 16, "resource": "inventory",
        "failure_code": "dependency_timeout",
        "occurred_at": datetime.fromtimestamp(START + 3, UTC)})
    def span(ident, parent, kind, start, end, event=None, status=None):
        attributes = []
        if event:
            attributes = [
                {"key": "aegisops.failure_code", "value": {"stringValue": event.failure_code}},
                {"key": "aegisops.resource", "value": {"stringValue": event.resource}},
            ]
        if status:
            attributes.append({"key": "http.status_code", "value": {"intValue": str(status)}})
        return {"traceId": binary(TRACE), "spanId": binary(ident),
                "parentSpanId": binary(parent) if parent else None, "kind": kind,
                "startTimeUnixNano": str(int((START + start) * 1e9)),
                "endTimeUnixNano": str(int((START + end) * 1e9)), "attributes": attributes}
    batches = [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "gateway"}},
    ]}, "scopeSpans": [{"spans": [span("1" * 16, None, "SPAN_KIND_SERVER", 0, 4,
                                        upstream, 502)]}]},
        {"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "orders"}},
        ]}, "scopeSpans": [{"spans": [
            span("2" * 16, "1" * 16, "SPAN_KIND_SERVER", 0.1, 3.6, status=502),
            span("3" * 16, "2" * 16, "SPAN_KIND_INTERNAL", 0.2, 3.5, downstream),
        ]}]}]
    return [upstream, downstream], batches


def test_valid_nested_request_link_remains_unbound_without_root_claim():
    events, batches = sample()
    links = links_for_events(events, batches)
    assert len(links) == 1 and links[0].span_path == ["1" * 16, "2" * 16, "3" * 16]
    assert links[0].downstream_event_id == events[1].id
    assert links[0].relationship_status == "VERIFIED_REQUEST_ANCESTRY"
    assert links[0].alert_binding_status == "UNBOUND"
    assert links[0].root_cause_status == "UNDETERMINED"


def test_dependency_error_response_links_to_nested_database_failure():
    events, batches = sample()
    events[0].failure_code = "dependency_http_5xx"
    events[1].failure_code = "database_lock_timeout"
    events[1].resource = "lab-orders-postgres"
    batches[0]["scopeSpans"][0]["spans"][0]["attributes"][0]["value"]["stringValue"] = (
        events[0].failure_code)
    attrs = batches[1]["scopeSpans"][0]["spans"][1]["attributes"]
    attrs[0]["value"]["stringValue"] = events[1].failure_code
    attrs[1]["value"]["stringValue"] = events[1].resource
    links = links_for_events(events, batches)
    assert len(links) == 1 and links[0].downstream_event_id == events[1].id
    assert links[0].downstream_failure_code == "database_lock_timeout"
    assert links[0].root_cause_status == "UNDETERMINED"
    assert links[0].alert_binding_status == "UNBOUND"


def test_timeout_without_error_response_does_not_claim_database_response_link():
    events, batches = sample()
    events[0].failure_code = "dependency_timeout"
    batches[0]["scopeSpans"][0]["spans"][0]["attributes"][0]["value"]["stringValue"] = (
        events[0].failure_code)
    assert links_for_events(events, batches) == []


@pytest.mark.parametrize("mutation", ["sibling", "missing_parent", "cycle", "healthy_response",
                                     "wrong_resource", "wrong_fact", "wrong_kind", "bad_timing"])
def test_same_trace_negative_cases_do_not_link(mutation):
    events, batches = sample()
    spans = batches[1]["scopeSpans"][0]["spans"]
    if mutation == "sibling":
        root = deepcopy(batches[0]["scopeSpans"][0]["spans"][0])
        root["spanId"] = binary("9" * 16)
        batches[0]["scopeSpans"][0]["spans"].append(root)
        batches[0]["scopeSpans"][0]["spans"][0]["parentSpanId"] = root["spanId"]
        spans[0]["parentSpanId"] = root["spanId"]
    elif mutation == "missing_parent":
        spans[0]["parentSpanId"] = binary("9" * 16)
    elif mutation == "cycle":
        spans[0]["parentSpanId"] = spans[1]["spanId"]
    elif mutation == "healthy_response":
        spans[0]["attributes"][0]["value"]["intValue"] = "200"
    elif mutation == "wrong_resource":
        events[0] = events[0].model_copy(update={"resource": "foreign"})
    elif mutation == "wrong_fact":
        spans[1]["attributes"][0]["value"]["stringValue"] = "other"
    elif mutation == "wrong_kind":
        spans[0]["kind"] = "SPAN_KIND_INTERNAL"
    else:
        spans[1]["endTimeUnixNano"] = str((START + 5) * 1_000_000_000)
    assert links_for_events(events, batches) == []


def test_error_event_order_must_follow_downstream_response():
    events, batches = sample()
    events[0] = events[0].model_copy(update={"occurred_at": datetime.fromtimestamp(START + 2, UTC)})
    assert links_for_events(events, batches) == []


def test_duplicate_and_mixed_identifiers_fail_closed():
    events, batches = sample()
    batches[1]["scopeSpans"][0]["spans"].append(
        deepcopy(batches[1]["scopeSpans"][0]["spans"][0]))
    with pytest.raises(RequestLinkUnverifiable):
        links_for_events(events, batches)
    events, batches = sample()
    batches[1]["scopeSpans"][0]["spans"][0]["traceId"] = binary("b" * 32)
    with pytest.raises(RequestLinkUnverifiable):
        links_for_events(events, batches)


def test_multiple_verified_downstream_failures_are_ambiguous():
    events, batches = sample()
    extra = events[1].model_copy(update={"id": uuid4(), "span_id": "4" * 16})
    span = deepcopy(batches[1]["scopeSpans"][0]["spans"][1])
    span["spanId"] = binary(extra.span_id)
    batches[1]["scopeSpans"][0]["spans"].append(span)
    assert links_for_events([*events, extra], batches) == []


def test_candidate_limits_and_naive_time_are_rejected():
    events, batches = sample()
    with pytest.raises(RequestLinkUnverifiable):
        links_for_events([events[0].model_copy(update={"id": uuid4()}) for _ in range(17)], batches)
    events[0] = events[0].model_copy(update={"occurred_at": datetime.now()})
    with pytest.raises(RequestLinkUnverifiable):
        links_for_events(events, batches)


def test_large_trace_is_rejected_before_partial_relationship_output():
    events, batches = sample()
    spans = batches[1]["scopeSpans"][0]["spans"]
    for number in range(4, 259):
        extra = deepcopy(spans[0])
        extra["spanId"] = binary(f"{number:016x}")
        spans.append(extra)
    with pytest.raises(RequestLinkUnverifiable, match="span limit"):
        links_for_events(events, batches)
