"""A request event is bound to its own service span and typed failure fact."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegisops.application.request_events import (
    RequestFailureEventInput,
    RequestFailureEventView,
    events_for_trace,
    trace_fact_status,
)


def event_input(**overrides: object) -> dict:
    return {
        "source_event_id": uuid4(),
        "service": "order-service",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "failure_code": "dependency_transport",
        "resource": "payment-service",
        "occurred_at": datetime.now(UTC),
        **overrides,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"trace_id": "z" * 32},
        {"span_id": "not-hex"},
        {"service": "order-service\n"},
        {"failure_code": "restart everything"},
        {"occurred_at": datetime.now(UTC) - timedelta(hours=1)},
        {"occurred_at": datetime.now().replace(tzinfo=None)},
    ],
)
def test_request_event_rejects_bad_provenance(override: dict) -> None:
    with pytest.raises(ValidationError):
        RequestFailureEventInput.model_validate(event_input(**override))


def test_trace_lookup_rejects_invalid_id_before_database_access() -> None:
    with pytest.raises(ValueError, match="invalid trace ID"):
        events_for_trace(None, "not-a-trace")  # type: ignore[arg-type]


def test_trace_verification_requires_exact_service_span_and_fact() -> None:
    body = RequestFailureEventInput.model_validate(event_input())
    event = RequestFailureEventView(
        id=uuid4(), source_event_id=body.source_event_id,
        service=body.service, trace_id=body.trace_id, span_id=body.span_id,
        failure_code=body.failure_code, resource=body.resource,
        occurred_at=body.occurred_at, provenance_status="SOURCE_ASSERTED",
    )
    batch = {
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": body.service}}
        ]},
        "scopeSpans": [{"spans": [{
            "spanId": base64.b64encode(bytes.fromhex(body.span_id)).decode(),
            "attributes": [
                {"key": "aegisops.failure_code", "value": {"stringValue": body.failure_code}},
                {"key": "aegisops.resource", "value": {"stringValue": body.resource}},
            ],
        }]}],
    }
    assert trace_fact_status([batch], event) == "VERIFIED_SPAN_FACT"
    batch["scopeSpans"][0]["spans"][0]["attributes"][0]["value"]["stringValue"] = "other"
    assert trace_fact_status([batch], event) == "SPAN_FACT_MISMATCH"
    batch["resource"]["attributes"][0]["value"]["stringValue"] = "other-service"
    assert trace_fact_status([batch], event) == "SPAN_NOT_FOUND"
