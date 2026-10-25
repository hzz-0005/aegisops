"""Reject mismatched trace facts and avoid promoting cancellation into lock evidence."""

import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aegisops.application.investigation.database_context import database_span_fact
from aegisops.application.request_events import RequestFailureEventView


def sample():
    now = datetime.now(UTC)
    event = RequestFailureEventView(id=uuid4(), source_event_id=uuid4(),
        service="inventory-service", resource="lab-orders-postgres", trace_id="a" * 32,
        span_id="b" * 16, occurred_at=now, failure_code="database_lock_timeout",
        provenance_status="SOURCE_ASSERTED")
    strings = {"aegisops.failure_code": event.failure_code, "aegisops.resource": event.resource,
        "db.system.name": "postgresql", "db.operation.name": "SELECT",
        "aegisops.db.operation": "inventory.lookup", "db.response.status_code": "55P03"}
    span = {"spanId": base64.b64encode(bytes.fromhex(event.span_id)).decode(),
        "traceId": base64.b64encode(bytes.fromhex(event.trace_id)).decode(),
        "startTimeUnixNano": str(int((now - timedelta(seconds=1.1)).timestamp() * 1e9)),
        "endTimeUnixNano": str(int((now + timedelta(milliseconds=1)).timestamp() * 1e9)),
        "status": {"code": "STATUS_CODE_ERROR"},
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in strings.items()]
           + [{"key": k, "value": {"doubleValue": v}} for k, v in {
               "aegisops.db.lock_timeout_ms": 1000, "aegisops.db.statement_timeout_ms": 2000,
               "aegisops.db.duration_ms": 1101}.items()]}
    batch = {"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": event.service}}]},
        "scopeSpans": [{"spans": [span]}]}
    return event, [batch], span


def test_exact_lock_fact_is_selected_without_sql_or_credentials():
    event, batches, span = sample()
    span["attributes"].append({"key": "db.statement", "value": {"stringValue": "SECRET"}})
    fact = database_span_fact(batches, event)
    assert fact is not None and fact["sqlstate"] == "55P03"
    assert "SECRET" not in str(fact)


@pytest.mark.parametrize("mutation", ["trace", "service", "time", "state", "sqlstate",
                                     "duration", "limits", "missing"])
def test_rejects_unverifiable_database_fact(mutation):
    event, batches, span = sample()
    if mutation == "trace":
        span["traceId"] = base64.b64encode(bytes.fromhex("c" * 32)).decode()
    elif mutation == "service":
        batches[0]["resource"]["attributes"][0]["value"]["stringValue"] = "foreign"
    elif mutation == "time":
        span["endTimeUnixNano"] = span["startTimeUnixNano"]
    elif mutation == "state":
        span["status"]["code"] = "STATUS_CODE_OK"
    elif mutation == "missing":
        del span["startTimeUnixNano"]
    else:
        key, value = {"sqlstate": ("db.response.status_code", {"stringValue": "57014"}),
                      "duration": ("aegisops.db.duration_ms", {"doubleValue": float("nan")}),
                      "limits": ("aegisops.db.lock_timeout_ms", {"intValue": "2000"})}[mutation]
        next(a for a in span["attributes"] if a["key"] == key)["value"] = value
    assert database_span_fact(batches, event) is None


def test_query_cancellation_is_preserved_as_cancellation():
    event, batches, _ = sample()
    event.failure_code = "database_query_cancelled"
    changed = deepcopy(batches)
    for attr in changed[0]["scopeSpans"][0]["spans"][0]["attributes"]:
        if attr["key"] == "aegisops.failure_code":
            attr["value"]["stringValue"] = event.failure_code
        if attr["key"] == "db.response.status_code":
            attr["value"]["stringValue"] = "57014"
    assert database_span_fact(changed, event)["sqlstate"] == "57014"


def test_database_advice_cites_fact_and_never_authorizes_action():
    from aegisops.application.investigation.advice import advice_from_report
    from tests.test_investigation_advice import report_with_context

    report = report_with_context()
    fact = report.evidence[0].model_copy(update={"kind": "VERIFIED_DATABASE_OPERATION",
        "polarity": "CONTEXT", "payload": {"failure_code": "database_lock_timeout"}})
    report.evidence.append(fact)
    advice = advice_from_report(report)
    assert advice.items[0].title == "核对数据库锁等待"
    assert advice.items[0].cited_evidence_ids == [fact.id]
    assert not advice.execution_allowed
    fact.payload["failure_code"] = "database_query_cancelled"
    assert advice_from_report(report).items[0].title == "核对数据库操作失败"


def test_recent_unexported_candidates_do_not_hide_older_exact_facts(monkeypatch):
    import httpx

    from aegisops.application.investigation import read_tools as module
    from aegisops.config import Settings

    event, batches, _ = sample()
    tenant = uuid4()
    pending = [event.model_copy(update={"trace_id": f"{i:032x}"}) for i in range(1, 5)]
    older = [event.model_copy(update={"id": uuid4()}) for _ in range(5)]
    calls = []
    def candidates(_settings, **scope):
        assert scope["tenant_id"] == tenant and scope["services"] == [event.service]
        return pending + older
    def source(_base, path, _params):
        calls.append(path)
        if path.endswith(event.trace_id):
            return {"batches": batches}
        response = httpx.Response(404, request=httpx.Request("GET", "http://tempo" + path))
        response.raise_for_status()
    monkeypatch.setattr(module, "database_candidates", candidates)
    monkeypatch.setattr(module, "trace_events", lambda *_args, **_kwargs: [])
    with module.ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", source)
        facts = tools.generic_database_requests(tenant_id=tenant, services=[event.service],
                                               occurred_at=event.occurred_at)
    assert len(calls) == 8 and len(facts) == 4
    assert all(f.polarity == "CONTEXT" and f.payload["current_state"] == "UNKNOWN" for f in facts)


def test_trace_source_errors_other_than_not_ready_remain_visible(monkeypatch):
    import httpx

    from aegisops.application.investigation import read_tools as module
    from aegisops.config import Settings

    def broken(*_):
        httpx.Response(503, request=httpx.Request("GET", "http://tempo")).raise_for_status()
    with module.ReadOnlyTelemetryTools(Settings()) as tools:
        monkeypatch.setattr(tools, "_json", broken)
        with pytest.raises(httpx.HTTPStatusError):
            tools._request_trace_if_ready("a" * 32)
