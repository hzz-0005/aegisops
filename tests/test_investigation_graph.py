"""A deployment hypothesis needs a matching change, beyond an alert and metric."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aegisops.application.investigation import graph as module
from aegisops.application.investigation.contracts import Evidence
from aegisops.application.investigation.signal_catalog import SignalCatalog, SignalDefinition
from aegisops.config import Settings


class FakeTelemetry:
    def generic_incoming_requests(self, **_: object) -> list[Evidence]:
        return []

    def generic_database_requests(self, **_: object) -> list[Evidence]:
        return []

    def request_alert_membership(self, **_: object) -> list[Evidence]:
        return []

    def __init__(self, _: Settings) -> None:
        pass

    def __enter__(self) -> "FakeTelemetry":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def metric(self, signal: SignalDefinition, **_: object) -> Evidence:
        return Evidence(
            source_type="METRIC",
            source_ref="test:counter",
            kind="COUNTER_INCREASE",
            subject=signal.hypothesis_key,
            polarity="SUPPORT",
            observed_at=datetime.now(UTC),
            collected_at=datetime.now(UTC),
            tool_name="test.metric",
            tool_version="1",
            payload={"peak_increase": 3},
        )

    def logs(self, *_: object, **__: object) -> list[Evidence]:
        return []

    def traces(self, *_: object, **__: object) -> list[Evidence]:
        return []

    def generic_http_activity(self, **_: object) -> Evidence:
        return Evidence(
            source_type="METRIC",
            source_ref="test:generic-5xx",
            kind="HTTP_REQUEST_ACTIVITY",
            subject="unclassified",
            polarity="CONTEXT",
            observed_at=datetime.now(UTC),
            collected_at=datetime.now(UTC),
            tool_name="test.generic_http_activity",
            tool_version="1",
            payload={"request_peak_increase": 2, "error_peak_increase": 0},
        )

    def generic_runtime_state(self, **_: object) -> list[Evidence]:
        return []

    def generic_request_links(self, **_: object) -> list[Evidence]:
        return []

    def generic_dependency_requests(self, **_: object) -> list[Evidence]:
        return []

    def generic_probe_health(self, **_: object) -> list[Evidence]:
        return []

    def generic_error_logs(self, **_: object) -> list[Evidence]:
        return [
            Evidence(
                source_type="LOG",
                source_ref="test:error-log",
                kind="ERROR_LOG_ACTIVITY",
                subject="unclassified",
                polarity="CONTEXT",
                observed_at=datetime.now(UTC),
                collected_at=datetime.now(UTC),
                tool_name="test.generic_error_logs",
                tool_version="1",
                payload={"matching_log_count_capped": 1},
            )
        ]

    def generic_error_traces(self, **_: object) -> list[Evidence]:
        return [
            Evidence(
                source_type="TRACE",
                source_ref="test:error-trace",
                kind="ERROR_SPAN_ACTIVITY",
                subject="unclassified",
                polarity="CONTEXT",
                observed_at=datetime.now(UTC),
                collected_at=datetime.now(UTC),
                tool_name="test.generic_error_traces",
                tool_version="1",
                payload={"error_span_count": 1},
            )
        ]


def test_unknown_alert_collects_context_without_claiming_root_cause(monkeypatch) -> None:
    now = datetime.now(UTC)
    tenant_id, incident_id = uuid4(), uuid4()
    snapshot: module.IncidentSnapshot = {
        "id": incident_id,
        "tenant_id": tenant_id,
        "version": 1,
        "title": "New operational symptom",
        "alerts": [
            {
                "id": uuid4(),
                "source_event_id": "unknown-event",
                "alert_name": "QueueLagSpike",
                "service": "payment-service",
                "resource": "queue-orders",
                "change_id": None,
                "occurred_at": now,
                "status": "FIRING",
            }
        ],
        "changes": {},
        "service_ids": {"payment-service": str(uuid4())},
        "dependencies": {"payment-service": ["redis-main"]},
        "recent_changes": {
            "payment-service": [
                {
                    "id": str(uuid4()),
                    "change_type": "CONFIGURATION",
                    "occurred_at": now - timedelta(minutes=2),
                }
            ]
        },
    }
    monkeypatch.setattr(module, "load_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(module, "ReadOnlyTelemetryTools", FakeTelemetry)
    graph = module.build_investigation_graph(
        Settings(investigation_agent_enabled=False), SignalCatalog(schema_version=1, signals=[])
    )
    report = graph.invoke({"tenant_id": tenant_id, "incident_id": incident_id})["report"]
    assert report.status == "INCONCLUSIVE"
    assert "现有证据不足" in report.summary
    assert report.primary_hypothesis is None
    assert {item.kind for item in report.evidence} == {
        "NORMALIZED_ALERT",
        "SERVICE_DEPENDENCIES",
        "HTTP_REQUEST_ACTIVITY",
        "RECENT_SERVICE_CHANGE",
        "ERROR_LOG_ACTIVITY",
        "ERROR_SPAN_ACTIVITY",
    }
    assert all(
        item.polarity == "CONTEXT" for item in report.evidence if item.kind != "NORMALIZED_ALERT"
    )
    assert any("no signal definition" in item for item in report.limitations)


def test_oom_signal_is_confirmed_event_but_not_assumed_root_cause(monkeypatch) -> None:
    now = datetime.now(UTC)
    tenant_id, incident_id = uuid4(), uuid4()
    snapshot: module.IncidentSnapshot = {
        "id": incident_id, "tenant_id": tenant_id, "version": 1,
        "title": "Container OOM event",
        "alerts": [{
            "id": uuid4(), "source_event_id": "oom-event",
            "alert_name": "LabContainerOOMKill", "service": "order-service",
            "resource": "order-service", "change_id": None,
            "occurred_at": now, "status": "FIRING",
        }],
        "changes": {}, "service_ids": {"order-service": str(uuid4())},
        "dependencies": {"order-service": []},
    }
    monkeypatch.setattr(module, "load_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(module, "ReadOnlyTelemetryTools", FakeTelemetry)
    signal = SignalDefinition(
        alert_name="LabContainerOOMKill", service="*",
        hypothesis_key="container_oom_event", hypothesis_label="OOM event",
        metric_name="lab_container_oom_kills_total", root_candidate=False,
    )
    catalog = SignalCatalog(schema_version=1, signals=[signal])
    assert catalog.find("LabContainerOOMKill", "order-service") == signal
    exact = signal.model_copy(update={"service": "order-service"})
    assert SignalCatalog(schema_version=1, signals=[signal, exact]).find(
        "LabContainerOOMKill", "order-service"
    ) == exact
    report = module.build_investigation_graph(
        Settings(investigation_agent_enabled=False), catalog
    ).invoke({
        "tenant_id": tenant_id, "incident_id": incident_id,
    })["report"]
    assert report.status == "INCONCLUSIVE"
    assert report.primary_hypothesis is None
    assert "OOM" in report.summary
    assert any(item.kind == "COUNTER_INCREASE" for item in report.evidence)


def test_deployment_requires_matching_change(monkeypatch) -> None:
    now = datetime.now(UTC)
    tenant_id, incident_id, service_id = uuid4(), uuid4(), uuid4()
    snapshot: module.IncidentSnapshot = {
        "id": incident_id,
        "tenant_id": tenant_id,
        "version": 1,
        "title": "Deployment failure",
        "alerts": [
            {
                "id": uuid4(),
                "source_event_id": "test-event",
                "alert_name": "OrderBadDeployment",
                "service": "order-service",
                "resource": None,
                "change_id": "change-1",
                "occurred_at": now,
                "status": "FIRING",
            }
        ],
        "changes": {},
        "service_ids": {"order-service": str(service_id)},
        "dependencies": {"order-service": []},
    }
    monkeypatch.setattr(module, "load_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(module, "ReadOnlyTelemetryTools", FakeTelemetry)
    signal = SignalDefinition(
        alert_name="OrderBadDeployment",
        service="order-service",
        hypothesis_key="bad_order_deployment",
        hypothesis_label="Faulty Order deployment",
        metric_name="failures_total",
        requires_change=True,
    )
    graph = module.build_investigation_graph(
        Settings(investigation_agent_enabled=False),
        SignalCatalog(schema_version=1, signals=[signal])
    )

    def status() -> str:
        return graph.invoke({"tenant_id": tenant_id, "incident_id": incident_id})["report"].status

    assert status() == "INCONCLUSIVE"
    snapshot["changes"]["change-1"] = {
        "id": str(uuid4()),
        "external_id": "change-1",
        "change_type": "DEPLOYMENT",
        "occurred_at": now - timedelta(minutes=2),
        "service_id": str(uuid4()),
        "from_ref": "v1",
        "to_ref": "v2",
    }
    assert status() == "INCONCLUSIVE"
    snapshot["changes"]["change-1"]["service_id"] = str(service_id)
    assert status() == "SUPPORTED"
    supported = graph.invoke({"tenant_id": tenant_id, "incident_id": incident_id})["report"]
    assert supported.summary.startswith("可能根因：")
    snapshot["changes"]["change-1"]["occurred_at"] = now - timedelta(hours=1)
    assert status() == "INCONCLUSIVE"


def test_paused_runtime_is_named_observation_without_root_claim(monkeypatch) -> None:
    now = datetime.now(UTC)
    tenant_id, incident_id = uuid4(), uuid4()
    snapshot: module.IncidentSnapshot = {
        "id": incident_id, "tenant_id": tenant_id, "version": 1,
        "title": "Unreachable service", "changes": {}, "service_ids": {},
        "dependencies": {"inventory-service": []}, "alerts": [{
            "id": uuid4(), "source_event_id": "availability", "alert_name": "UnmappedAvailability",
            "service": "inventory-service", "resource": "inventory-service", "change_id": None,
            "occurred_at": now, "status": "FIRING",
        }],
    }
    class RuntimeTelemetry(FakeTelemetry):
        def generic_runtime_state(self, **_: object) -> list[Evidence]:
            return [Evidence(
                source_type="METRIC", source_ref="test:paused", kind="RUNTIME_STATE_OBSERVATION",
                subject="unclassified", polarity="CONTEXT", observed_at=now, collected_at=now,
                tool_name="test.runtime", tool_version="1",
                payload={"service": "inventory-service", "state": "paused", "sample_count": 1},
            )]
    monkeypatch.setattr(module, "load_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(module, "ReadOnlyTelemetryTools", RuntimeTelemetry)
    report = module.build_investigation_graph(
        Settings(investigation_agent_enabled=False), SignalCatalog(schema_version=1, signals=[]),
    )
    result = report.invoke({"tenant_id": tenant_id, "incident_id": incident_id})["report"]
    assert "暂停" in result.summary
    assert result.status == "INCONCLUSIVE" and result.primary_hypothesis is None
