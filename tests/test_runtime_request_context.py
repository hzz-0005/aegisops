"""Lifecycle facts and exact request facts must not authorize aggregate repairs."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from aegisops.application.investigation import read_tools as module
from aegisops.application.investigation.advice import advice_from_report
from aegisops.application.request_events import RequestFailureEventView
from aegisops.config import Settings
from apps.lab_executor.runtime_metrics import observe
from tests.test_investigation_advice import report_with_context


def test_observer_only_reads_allowlisted_identity_and_selects_state():
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={
            "Config": {"Env": ["SECRET=must-never-escape"], "Labels": {
                "com.docker.compose.project": "aegisops",
                "com.docker.compose.service": "inventory-service",
            }},
            "State": {"Status": "paused", "Paused": True,
                      "StartedAt": "2026-10-08T01:00:00Z"},
        })
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = observe(client, "inventory-service")
        assert result["state"] == "paused" and "Config" not in result
        assert calls == [("GET", "/containers/aegisops-inventory-service-1/json")]
        with pytest.raises(KeyError):
            observe(client, "postgres")


def test_observer_rejects_foreign_container_identity():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "Config": {"Labels": {"com.docker.compose.project": "foreign"}},
    }))) as client:
        with pytest.raises(ValueError):
            observe(client, "inventory-service")


def test_runtime_filter_uses_freshness_and_keeps_running_as_context(monkeypatch):
    now = datetime.now(UTC)
    def response(_base, _path, params):
        assert "lab_runtime_observed == 1" in params["query"]
        assert "lab_runtime_observed_at_seconds < 15" in params["query"]
        return {"data": {"result": [
            {"metric": {"service": "inventory-service", "state": "paused"},
             "values": [[now.timestamp(), "1"]]},
            {"metric": {"service": "order-service", "state": "running"},
             "values": [[now.timestamp(), "1"]]},
            {"metric": {"service": "foreign", "state": "paused"},
             "values": [[now.timestamp(), "1"]]},
        ]}}
    with module.ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", response)
        facts = tools.generic_runtime_state(services=["inventory-service", "order-service"],
                                            occurred_at=now)
    assert len(facts) == 2 and all(f.polarity == "CONTEXT" for f in facts)
    advice = advice_from_report(report_with_context().model_copy(update={"evidence": facts}))
    assert "暂停" in advice.items[0].rationale and not advice.execution_allowed


@pytest.mark.parametrize("failure_code", ["dependency_timeout", "upstream_http_5xx",
                                         "upstream_transport"])
def test_dependency_fact_requires_exact_span_and_never_binds_alert(monkeypatch, failure_code):
    now = datetime.now(UTC)
    event = RequestFailureEventView(id=uuid4(), source_event_id=uuid4(), service="order-service",
        trace_id="a" * 32, span_id="b" * 16, failure_code=failure_code,
        resource="inventory-service", occurred_at=now, provenance_status="SOURCE_ASSERTED")
    candidates = [event, event.model_copy(update={"service": "foreign"}),
                  event.model_copy(update={"occurred_at": now - timedelta(minutes=2)})]
    def load(_settings, **scope):
        assert scope["tenant_id"] == tenant and scope["dependencies"] == ["inventory-service"]
        return candidates
    tenant = uuid4()
    monkeypatch.setattr(module, "dependency_candidates", load)
    batch = {"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": event.service}},
    ]}, "scopeSpans": [{"spans": [{
        "spanId": base64.b64encode(bytes.fromhex(event.span_id)).decode(), "attributes": [
            {"key": "aegisops.failure_code", "value": {"stringValue": event.failure_code}},
            {"key": "aegisops.resource", "value": {"stringValue": event.resource}},
        ],
    }]}]}
    with module.ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", lambda *_: {"batches": [batch]})
        facts = tools.generic_dependency_requests(tenant_id=tenant, service=event.service,
                    dependencies=["inventory-service"], occurred_at=now)
        assert len(facts) == 1 and facts[0].payload["alert_binding_status"] == "UNBOUND"
        assert facts[0].polarity == "CONTEXT"
        batch["scopeSpans"][0]["spans"][0]["attributes"][0]["value"]["stringValue"] = "wrong"
        assert tools.generic_dependency_requests(tenant_id=tenant, service=event.service,
                    dependencies=["inventory-service"], occurred_at=now) == []


@pytest.mark.parametrize("failure_code", ["upstream_http_5xx", "upstream_transport"])
def test_incoming_gateway_failure_requires_exact_span(monkeypatch, failure_code):
    now, tenant = datetime.now(UTC), uuid4()
    event = RequestFailureEventView(id=uuid4(), source_event_id=uuid4(), service="gateway",
        trace_id="a" * 32, span_id="b" * 16, failure_code=failure_code,
        resource="order-service", occurred_at=now, provenance_status="SOURCE_ASSERTED")
    def candidates(_settings, **scope):
        assert scope == {"tenant_id": tenant, "service": "order-service", "occurred_at": now}
        return [event]
    monkeypatch.setattr(module, "incoming_candidates", candidates)
    batch = {"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": event.service}},
    ]}, "scopeSpans": [{"spans": [{
        "spanId": base64.b64encode(bytes.fromhex(event.span_id)).decode(), "attributes": [
            {"key": "aegisops.failure_code", "value": {"stringValue": failure_code}},
            {"key": "aegisops.resource", "value": {"stringValue": event.resource}},
        ],
    }]}]}
    with module.ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", lambda *_: {"batches": [batch]})
        facts = tools.generic_incoming_requests(tenant_id=tenant, service="order-service",
                                               occurred_at=now)
        assert len(facts) == 1 and facts[0].payload["failure_code"] == failure_code
        assert facts[0].payload["alert_binding_status"] == "UNBOUND"
        batch["scopeSpans"][0]["spans"][0]["attributes"][0]["value"]["stringValue"] = "wrong"
        assert tools.generic_incoming_requests(tenant_id=tenant, service="order-service",
                                               occurred_at=now) == []


def test_request_links_limit_queries_and_preserve_tenant_scope(monkeypatch):
    from tests.test_request_failure_links import sample

    events, batches = sample()
    tenant = uuid4()
    candidates = [events[0].model_copy(update={"trace_id": f"{i:032x}"}) for i in range(1, 7)]
    queries = []
    def candidates_for(_settings, **scope):
        assert scope["tenant_id"] == tenant
        return candidates
    def events_for(_settings, **scope):
        assert scope["tenant_id"] == tenant
        return events
    def response(_base, path, _params):
        queries.append(path)
        return {"batches": batches}
    monkeypatch.setattr(module, "recent_link_candidates", candidates_for)
    monkeypatch.setattr(module, "trace_events", events_for)
    with module.ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", response)
        facts = tools.generic_request_links(tenant_id=tenant, service="gateway",
                                           occurred_at=events[0].occurred_at)
    assert len(queries) == 4
    assert facts == []  # A different trace returned by the source cannot match a candidate.
