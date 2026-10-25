"""Unknown-signal telemetry must stay bounded and avoid copying raw content."""

from datetime import UTC, datetime

import pytest

from aegisops.application.investigation.read_tools import ReadOnlyTelemetryTools
from aegisops.config import Settings


def test_generic_error_logs_keep_only_activity_metadata(monkeypatch) -> None:
    secret = "customer-token-should-not-appear"
    now = datetime.now(UTC)
    timestamp = str(int(now.timestamp() * 1_000_000_000))
    with ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(
            tools,
            "_json",
            lambda *_: {"data": {"result": [
                {"stream": {"detected_level": "error"}, "values": [[timestamp, secret]]},
                {"stream": {"detected_level": "info"}, "values": [[timestamp, secret]]},
            ]}},
        )
        evidence = tools.generic_error_logs(service="payment-service", occurred_at=now)
        assert len(evidence) == 1
        assert evidence[0].polarity == "CONTEXT"
        assert evidence[0].payload["matching_log_count_capped"] == 1
        assert secret not in evidence[0].model_dump_json()
        with pytest.raises(ValueError, match="service name"):
            tools.generic_error_logs(service='payment-service"} |= "secret"', occurred_at=now)


def test_generic_error_traces_ignore_other_services_and_span_names(monkeypatch) -> None:
    now = datetime.now(UTC)
    trace_id = "0123456789abcdef0123456789abcdef"
    secret = "sensitive-span-name"
    search = {
        "traces": [{"traceID": trace_id, "startTimeUnixNano": str(int(now.timestamp() * 1e9))}]
    }
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "other"}}]
                },
                "scopeSpans": [
                    {"spans": [{"name": secret, "status": {"code": "STATUS_CODE_ERROR"}}]}
                ],
            },
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "payment-service"}}
                    ]
                },
                "scopeSpans": [
                    {"spans": [{"name": secret, "status": {"code": "STATUS_CODE_ERROR"}}]}
                ],
            },
        ]
    }
    with ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(
            tools, "_json", lambda _, path, __: search if path == "/api/search" else trace
        )
        evidence = tools.generic_error_traces(service="payment-service", occurred_at=now)
        assert len(evidence) == 1
        assert evidence[0].payload["error_span_count"] == 1
        assert evidence[0].polarity == "CONTEXT"
        assert secret not in evidence[0].model_dump_json()
