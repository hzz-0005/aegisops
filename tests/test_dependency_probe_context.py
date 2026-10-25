"""Probe observations remain bounded context and never grant remediation authority."""

from datetime import UTC, datetime

import pytest

from aegisops.application.investigation.advice import advice_from_report
from aegisops.application.investigation.read_tools import ReadOnlyTelemetryTools
from aegisops.config import Settings
from tests.test_investigation_advice import report_with_context


def test_probe_filters_unrequested_services_and_invalid_samples(monkeypatch) -> None:
    now = datetime.now(UTC)
    with ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", lambda *_: {"data": {"result": [
            {"metric": {"service": "payment-service"}, "values": [
                [now.timestamp(), "0"], [now.timestamp(), "NaN"], [now.timestamp(), "1"],
            ]},
            {"metric": {"service": "foreign-service"}, "values": [[now.timestamp(), "0"]]},
        ]}})
        evidence = tools.generic_probe_health(services=["payment-service"], occurred_at=now)
        assert len(evidence) == 1
        assert evidence[0].payload == {
            "service": "payment-service", "sample_count": 2, "failure_sample_count": 1,
        }
        assert evidence[0].polarity == "CONTEXT"
        report = report_with_context().model_copy(update={"evidence": evidence})
        advice = advice_from_report(report)
        assert advice.mode == "MANUAL_INVESTIGATION" and advice.execution_allowed is False
        assert advice.items[0].cited_evidence_ids == [evidence[0].id]
        assert "不能证明进程停机" in advice.items[0].risk
        with pytest.raises(ValueError):
            tools.generic_probe_health(services=['bad"selector'], occurred_at=now)


def test_missing_probe_samples_are_not_healthy_evidence(monkeypatch) -> None:
    with ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", lambda *_: {"data": {"result": []}})
        assert tools.generic_probe_health(services=["order-service"],
                                          occurred_at=datetime.now(UTC)) == []


def test_failure_categories_remain_context_with_chinese_advice(monkeypatch) -> None:
    now = datetime.now(UTC)
    def response(_base, _path, params):
        if "lab_probe_failure" in params["query"]:
            return {"data": {"result": [
                {"metric": {"service": "payment-service", "reason": "timeout"},
                 "values": [[now.timestamp(), "1"], [now.timestamp(), "NaN"]]},
                {"metric": {"service": "foreign-service", "reason": "transport"},
                 "values": [[now.timestamp(), "1"]]},
                {"metric": {"service": "payment-service", "reason": "invented"},
                 "values": [[now.timestamp(), "1"]]},
            ]}}
        return {"data": {"result": [
            {"metric": {"service": "payment-service"}, "values": [[now.timestamp(), "0"]]},
        ]}}
    with ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", response)
        evidence = tools.generic_probe_health(services=["payment-service"], occurred_at=now)
    assert evidence[0].payload["failure_reason_counts"] == {"timeout": 1}
    assert evidence[0].polarity == "CONTEXT"
    report = report_with_context().model_copy(update={"evidence": evidence})
    advice = advice_from_report(report)
    assert "响应超时" in advice.items[0].rationale
    assert not advice.execution_allowed
