"""Bounded GET-only telemetry adapters. Inputs come from trusted signal definitions."""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from aegisops.application.request_events import trace_fact_status
from aegisops.application.request_failure_links import (
    RequestLinkUnverifiable,
    links_for_events,
    recent_link_candidates,
    trace_events,
)
from aegisops.application.request_signal_alerts import membership_evidence
from aegisops.config import Settings

from .contracts import Evidence
from .database_context import database_span_fact
from .request_context import (
    CLIENT_FAILURE_CODES,
    DATABASE_CANDIDATE_LIMIT,
    DATABASE_CODES,
    database_candidates,
    dependency_candidates,
    incoming_candidates,
)
from .signal_catalog import SignalDefinition

METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
TRACE_ID = re.compile(r"^[a-fA-F0-9]{16,32}$")
SERVICE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
TOOL_VERSION = "http-v1"


class ReadOnlyTelemetryTools:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.Client(timeout=settings.investigation_tool_timeout_seconds)
        self.deadline: float | None = None

    def __enter__(self) -> "ReadOnlyTelemetryTools":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def _json(self, base: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("investigation deadline reached")
            kwargs["timeout"] = min(self.settings.investigation_tool_timeout_seconds, remaining)
        response = self.client.get(f"{base.rstrip('/')}{path}", params=params, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("status") == "error":
            raise ValueError(f"unexpected {path} response")
        return data

    def metric(
        self,
        signal: SignalDefinition,
        *,
        service: str,
        change_id: str | None,
        occurred_at: datetime,
    ) -> Evidence | None:
        if not METRIC_NAME.fullmatch(signal.metric_name):
            raise ValueError("metric name is not allowed")
        labels = {"service": service}
        if signal.metric_change_label:
            if not change_id:
                return None
            labels["change_id"] = change_id
        selector = ",".join(f"{key}={json.dumps(value)}" for key, value in labels.items())
        expression = f"sum(increase({signal.metric_name}{{{selector}}}[1m]))"
        start = occurred_at - timedelta(seconds=15)
        end = occurred_at + timedelta(seconds=90)
        data = self._json(
            self.settings.prometheus_url,
            "/api/v1/query_range",
            {
                "query": expression,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": 5,
            },
        )
        series = data.get("data", {}).get("result", [])
        samples = [
            (float(sample[0]), float(sample[1]))
            for item in series
            for sample in item.get("values", [])
            if len(sample) == 2 and sample[1] not in {"NaN", "+Inf", "-Inf"}
        ]
        if not samples:
            return None
        sample_time, peak = max(samples, key=lambda sample: sample[1])
        return Evidence(
            source_type="METRIC",
            source_ref=f"prometheus:{signal.metric_name}:{service}:{change_id or '-'}",
            kind="COUNTER_INCREASE",
            subject=signal.hypothesis_key,
            polarity="SUPPORT" if peak > 0.1 else "CONFLICT",
            observed_at=datetime.fromtimestamp(sample_time, UTC),
            collected_at=datetime.now(UTC),
            tool_name="prometheus.query_range",
            tool_version=TOOL_VERSION,
            payload={
                "metric": signal.metric_name,
                "labels": labels,
                "window": "1m",
                "peak_increase": round(peak, 4),
            },
        )

    def generic_http_activity(self, *, service: str, occurred_at: datetime) -> Evidence | None:
        """Read fixed Lab counters for unclassified alerts without inferring a cause."""
        selector = f"service={json.dumps(service)}"
        expression = f"sum(increase(lab_http_requests_total{{{selector}}}[1m]))"
        start = occurred_at - timedelta(seconds=15)
        end = occurred_at + timedelta(seconds=90)
        data = self._json(
            self.settings.prometheus_url,
            "/api/v1/query_range",
            {"query": expression, "start": start.timestamp(), "end": end.timestamp(), "step": 5},
        )
        samples = self._metric_samples(data)
        if not samples:
            return None
        sample_time, request_peak = max(samples, key=lambda sample: sample[1])
        error_expression = f'sum(increase(lab_http_requests_total{{{selector},status=~"5.."}}[1m]))'
        errors = self._json(
            self.settings.prometheus_url,
            "/api/v1/query_range",
            {
                "query": error_expression,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": 5,
            },
        )
        error_samples = self._metric_samples(errors)
        return Evidence(
            source_type="METRIC",
            source_ref=f"prometheus:lab_http_requests_total:{service}:all-statuses",
            kind="HTTP_REQUEST_ACTIVITY",
            subject="unclassified",
            polarity="CONTEXT",
            observed_at=datetime.fromtimestamp(sample_time, UTC),
            collected_at=datetime.now(UTC),
            tool_name="prometheus.query_range",
            tool_version=TOOL_VERSION,
            payload={
                "service": service,
                "request_peak_increase": round(request_peak, 4),
                "error_peak_increase": (
                    round(max(value for _, value in error_samples), 4) if error_samples else None
                ),
            },
        )

    @staticmethod
    def _metric_samples(data: dict[str, Any]) -> list[tuple[float, float]]:
        return [
            (float(sample[0]), float(sample[1]))
            for item in data.get("data", {}).get("result", [])
            for sample in item.get("values", [])
            if len(sample) == 2 and sample[1] not in {"NaN", "+Inf", "-Inf"}
        ]

    def generic_error_logs(self, *, service: str, occurred_at: datetime) -> list[Evidence]:
        """Return bounded error activity metadata, never untrusted log text."""
        if not SERVICE_NAME.fullmatch(service):
            raise ValueError("service name is not allowed")
        start = occurred_at - timedelta(minutes=1)
        end = occurred_at + timedelta(minutes=3)
        data = self._json(
            self.settings.loki_url,
            "/loki/api/v1/query_range",
            {
                "query": f"{{service_name={json.dumps(service)}}}",
                "start": int(start.timestamp() * 1_000_000_000),
                "end": int(end.timestamp() * 1_000_000_000),
                "limit": 20,
            },
        )
        timestamps = [
            int(timestamp)
            for stream in data.get("data", {}).get("result", [])
            if stream.get("stream", {}).get("detected_level", "").lower() in {"error", "fatal"}
            for timestamp, _ in stream.get("values", [])
        ][:5]
        if not timestamps:
            return []
        latest = max(timestamps)
        return [
            Evidence(
                source_type="LOG",
                source_ref=f"loki:{service}:error-activity:{latest}",
                kind="ERROR_LOG_ACTIVITY",
                subject="unclassified",
                polarity="CONTEXT",
                observed_at=datetime.fromtimestamp(latest / 1_000_000_000, UTC),
                collected_at=datetime.now(UTC),
                tool_name="loki.query_range",
                tool_version=TOOL_VERSION,
                payload={"service": service, "matching_log_count_capped": len(timestamps)},
            )
        ]

    def generic_probe_health(
        self, *, services: list[str], occurred_at: datetime,
    ) -> list[Evidence]:
        """Historical endpoint observations are context, never process or network root proof."""
        if any(not SERVICE_NAME.fullmatch(service) for service in services):
            raise ValueError("service name is not allowed")
        allowed = sorted(set(services))[:6]
        if not allowed:
            return []
        selector = json.dumps("|".join(allowed))
        data = self._json(self.settings.prometheus_url, "/api/v1/query_range", {
            "query": f"lab_probe_success{{service=~{selector}}}",
            "start": (occurred_at - timedelta(seconds=15)).timestamp(),
            "end": (occurred_at + timedelta(seconds=90)).timestamp(), "step": 5,
        })
        reason_data = self._json(self.settings.prometheus_url, "/api/v1/query_range", {
            "query": f"lab_probe_failure{{service=~{selector}}}",
            "start": (occurred_at - timedelta(seconds=15)).timestamp(),
            "end": (occurred_at + timedelta(seconds=90)).timestamp(), "step": 5,
        })
        reasons: dict[str, dict[str, int]] = {}
        for series in reason_data.get("data", {}).get("result", [])[:24]:
            labels = series.get("metric", {})
            service, reason = labels.get("service"), labels.get("reason")
            if service not in allowed or reason not in {"timeout", "transport", "http_status"}:
                continue
            count = sum(str(v) in {"1", "1.0"} for _, v in series.get("values", [])[:24])
            if count:
                reasons.setdefault(service, {})[reason] = count
        output = []
        seen = set()
        for series in data.get("data", {}).get("result", [])[:24]:
            service = series.get("metric", {}).get("service")
            if service not in allowed or service in seen:
                continue
            samples = [(float(t), float(v)) for t, v in series.get("values", [])
                       if str(v) in {"0", "1", "0.0", "1.0"}][:24]
            if not samples:
                continue
            seen.add(service)
            output.append(Evidence(
                source_type="METRIC", source_ref=f"prometheus:lab_probe_success:{service}",
                kind="DEPENDENCY_PROBE_ACTIVITY", subject="unclassified", polarity="CONTEXT",
                observed_at=datetime.fromtimestamp(max(t for t, _ in samples), UTC),
                collected_at=datetime.now(UTC), tool_name="prometheus.query_range",
                tool_version="probe-context-v2-outcomes", payload={
                    "service": service, "sample_count": len(samples),
                    "failure_sample_count": sum(v == 0 for _, v in samples),
                    **({"failure_reason_counts": reasons[service]} if service in reasons else {}),
                },
            ))
        return output

    def generic_runtime_state(
        self, *, services: list[str], occurred_at: datetime,
    ) -> list[Evidence]:
        if any(not SERVICE_NAME.fullmatch(service) for service in services):
            raise ValueError("service name is not allowed")
        allowed = sorted(set(services))[:6]
        if not allowed:
            return []
        selector = json.dumps("|".join(allowed))
        expression = (f"lab_runtime_state{{service=~{selector}}} == 1"
                      " and on(service) (lab_runtime_observed == 1)"
                      " and on(service) (time() - lab_runtime_observed_at_seconds >= 0)"
                      " and on(service) (time() - lab_runtime_observed_at_seconds < 15)")
        data = self._json(self.settings.prometheus_url, "/api/v1/query_range", {
            "query": expression, "start": (occurred_at - timedelta(seconds=15)).timestamp(),
            "end": occurred_at.timestamp(), "step": 5,
        })
        output = []
        for series in data.get("data", {}).get("result", [])[:48]:
            labels = series.get("metric", {})
            service, state = labels.get("service"), labels.get("state")
            if service not in allowed or state not in {
                "running", "paused", "exited", "restarting", "created", "dead", "removing",
            }:
                continue
            samples = [(float(t), float(v)) for t, v in series.get("values", [])[:4]
                       if str(v) in {"1", "1.0"}]
            if not samples:
                continue
            output.append(Evidence(
                source_type="METRIC", source_ref=f"prometheus:lab_runtime_state:{service}:{state}",
                kind="RUNTIME_STATE_OBSERVATION", subject="unclassified", polarity="CONTEXT",
                observed_at=datetime.fromtimestamp(max(t for t, _ in samples), UTC),
                collected_at=datetime.now(UTC), tool_name="prometheus.query_range",
                tool_version="runtime-context-v1", payload={"service": service, "state": state,
                                                           "sample_count": len(samples)},
            ))
        return output

    def _request_trace_if_ready(self, trace_id: str) -> dict[str, Any]:
        try:
            return self._json(self.settings.tempo_url, f"/api/traces/{trace_id}", {})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {}  # Event delivery can precede span export; try the next candidate.
            raise

    def generic_dependency_requests(
        self, *, tenant_id: UUID, service: str, dependencies: list[str], occurred_at: datetime,
    ) -> list[Evidence]:
        if any(not SERVICE_NAME.fullmatch(name) for name in [service, *dependencies]):
            raise ValueError("service name is not allowed")
        allowed = sorted(set(dependencies))[:6]
        if not allowed:
            return []
        candidates = dependency_candidates(self.settings, tenant_id=tenant_id, service=service,
                                           dependencies=allowed, occurred_at=occurred_at)
        output = []
        for event in candidates[:4]:
            if (event.service != service or event.resource not in allowed
                    or event.failure_code not in CLIENT_FAILURE_CODES
                    or abs((event.occurred_at - occurred_at).total_seconds()) > 30
                    or not TRACE_ID.fullmatch(event.trace_id)):
                continue
            trace_data = self._request_trace_if_ready(event.trace_id)
            if trace_fact_status(trace_data.get("batches", []), event) != "VERIFIED_SPAN_FACT":
                continue
            output.append(Evidence(
                source_type="TRACE", source_ref=f"request-event:{event.id}:{event.span_id}",
                kind="VERIFIED_DEPENDENCY_REQUEST", subject="unclassified", polarity="CONTEXT",
                observed_at=event.occurred_at, collected_at=datetime.now(UTC),
                tool_name="request_events_and_tempo", tool_version="dependency-context-v1",
                payload={"service": event.service, "dependency": event.resource,
                         "failure_code": event.failure_code, "event_id": str(event.id),
                         "trace_id": event.trace_id, "span_id": event.span_id,
                         "provenance_status": "VERIFIED_SPAN_FACT",
                         "alert_binding_status": "UNBOUND"},
            ))
        return output

    def generic_incoming_requests(
        self, *, tenant_id: UUID, service: str, occurred_at: datetime,
    ) -> list[Evidence]:
        if not SERVICE_NAME.fullmatch(service):
            raise ValueError("service name is not allowed")
        events = incoming_candidates(self.settings, tenant_id=tenant_id, service=service,
                                     occurred_at=occurred_at)
        output = []
        for event in events[:4]:
            if (event.resource != service or event.failure_code not in CLIENT_FAILURE_CODES
                    or abs((event.occurred_at - occurred_at).total_seconds()) > 30
                    or not TRACE_ID.fullmatch(event.trace_id)):
                continue
            data = self._request_trace_if_ready(event.trace_id)
            if trace_fact_status(data.get("batches", []), event) != "VERIFIED_SPAN_FACT":
                continue
            output.append(Evidence(
                source_type="TRACE", source_ref=f"request-event:{event.id}:{event.span_id}",
                kind="VERIFIED_DEPENDENCY_REQUEST", subject="unclassified", polarity="CONTEXT",
                observed_at=event.occurred_at, collected_at=datetime.now(UTC),
                tool_name="request_events_and_tempo", tool_version="incoming-context-v2",
                payload={"service": event.service, "dependency": service,
                         "failure_code": event.failure_code, "event_id": str(event.id),
                         "trace_id": event.trace_id, "span_id": event.span_id,
                         "query_direction": "INCOMING", "provenance_status": "VERIFIED_SPAN_FACT",
                         "alert_binding_status": "UNBOUND"},
            ))
        return output

    def generic_database_requests(
        self, *, tenant_id: UUID, services: list[str], occurred_at: datetime,
    ) -> list[Evidence]:
        if any(not SERVICE_NAME.fullmatch(name) for name in services):
            raise ValueError("service name is not allowed")
        allowed = sorted(set(services))[:7]
        events = database_candidates(self.settings, tenant_id=tenant_id, services=allowed,
                                     occurred_at=occurred_at)
        output: list[Evidence] = []
        for event in events[:DATABASE_CANDIDATE_LIMIT]:
            if len(output) >= 4:
                break
            if (event.service not in allowed or event.failure_code not in DATABASE_CODES
                    or abs((event.occurred_at - occurred_at).total_seconds()) > 30
                    or not TRACE_ID.fullmatch(event.trace_id)):
                continue
            data = self._request_trace_if_ready(event.trace_id)
            fact = database_span_fact(data.get("batches", []), event)
            if fact is None:
                continue
            caller_links = []
            try:
                events = trace_events(self.settings, tenant_id=tenant_id, trace_id=event.trace_id)
                caller_links = [link.model_dump(mode="json") for link in links_for_events(
                    events, data.get("batches", [])) if link.downstream_event_id == event.id]
            except RequestLinkUnverifiable:
                pass  # A bounded operation fact can exist without a verified request path.
            output.append(Evidence(
                source_type="TRACE", source_ref=f"request-event:{event.id}:{event.span_id}",
                kind="VERIFIED_DATABASE_OPERATION", subject="unclassified", polarity="CONTEXT",
                observed_at=event.occurred_at, collected_at=datetime.now(UTC),
                tool_name="request_events_and_tempo", tool_version="database-context-v2-ancestry",
                payload={**fact, "service": event.service, "resource": event.resource,
                         "failure_code": event.failure_code, "event_id": str(event.id),
                         "trace_id": event.trace_id, "span_id": event.span_id,
                         "provenance_status": "VERIFIED_SPAN_FACT",
                         "alert_binding_status": "UNBOUND", "current_state": "UNKNOWN",
                         "caller_links": caller_links,
                         "candidate_limit": DATABASE_CANDIDATE_LIMIT, "fact_limit": 4},
            ))
        return output

    def generic_request_links(
        self, *, tenant_id: UUID, service: str, occurred_at: datetime,
    ) -> list[Evidence]:
        if not SERVICE_NAME.fullmatch(service):
            raise ValueError("service name is not allowed")
        candidates = recent_link_candidates(self.settings, tenant_id=tenant_id, service=service,
                                            occurred_at=occurred_at)
        output = []
        seen = set()
        for event in candidates[:4]:
            if (event.trace_id in seen or event.service != service
                    or not TRACE_ID.fullmatch(event.trace_id)
                    or abs((event.occurred_at - occurred_at).total_seconds()) > 30):
                continue
            seen.add(event.trace_id)
            trace_data = self._request_trace_if_ready(event.trace_id)
            try:
                links = links_for_events(trace_events(
                    self.settings, tenant_id=tenant_id, trace_id=event.trace_id,
                ), trace_data.get("batches", []))
            except RequestLinkUnverifiable:
                continue
            for link in links:
                if (link.trace_id != event.trace_id
                        or event.id not in {link.downstream_event_id, link.upstream_event_id}):
                    continue
                output.append(Evidence(
                    source_type="TRACE",
                    source_ref=f"request-link:{link.upstream_event_id}:{link.downstream_event_id}",
                    kind="VERIFIED_REQUEST_FAILURE_LINK",
                    subject="unclassified", polarity="CONTEXT",
                    observed_at=link.observed_at, collected_at=datetime.now(UTC),
                    tool_name="request_events_and_tempo_ancestry", tool_version="request-links-v1",
                    payload=link.model_dump(mode="json"),
                ))
        return output

    def request_alert_membership(self, *, tenant_id: UUID, alert_id: UUID) -> list[Evidence]:
        return membership_evidence(self.settings, tenant_id=tenant_id, alert_id=alert_id)

    def generic_error_traces(self, *, service: str, occurred_at: datetime) -> list[Evidence]:
        """Count service error spans without exposing span names or attributes."""
        if not SERVICE_NAME.fullmatch(service):
            raise ValueError("service name is not allowed")
        start = occurred_at - timedelta(minutes=1)
        end = occurred_at + timedelta(minutes=3)
        data = self._json(
            self.settings.tempo_url,
            "/api/search",
            {
                "tags": f"service.name={service}",
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "limit": 8,
            },
        )
        output: list[Evidence] = []
        for item in data.get("traces", [])[:8]:
            trace_id = item.get("traceID", "")
            if not isinstance(trace_id, str) or not TRACE_ID.fullmatch(trace_id):
                continue
            trace = self._json(self.settings.tempo_url, f"/api/traces/{trace_id}", {})
            error_count = 0
            for batch in trace.get("batches", []):
                service_name = next(
                    (
                        attribute.get("value", {}).get("stringValue")
                        for attribute in batch.get("resource", {}).get("attributes", [])
                        if attribute.get("key") == "service.name"
                    ),
                    None,
                )
                if service_name != service:
                    continue
                error_count += sum(
                    span.get("status", {}).get("code") == "STATUS_CODE_ERROR"
                    for scope in batch.get("scopeSpans", [])
                    for span in scope.get("spans", [])
                )
            if not error_count:
                continue
            output.append(
                Evidence(
                    source_type="TRACE",
                    source_ref=f"tempo:{trace_id}",
                    kind="ERROR_SPAN_ACTIVITY",
                    subject="unclassified",
                    polarity="CONTEXT",
                    observed_at=datetime.fromtimestamp(
                        int(item["startTimeUnixNano"]) / 1_000_000_000, UTC
                    ),
                    collected_at=datetime.now(UTC),
                    tool_name="tempo.search_and_get_trace",
                    tool_version=TOOL_VERSION,
                    payload={"service": service, "error_span_count": error_count},
                )
            )
        return output

    def logs(
        self, signal: SignalDefinition, *, service: str, occurred_at: datetime
    ) -> list[Evidence]:
        if not signal.log_phrase:
            return []
        query = f"{{service_name={json.dumps(service)}}} |= {json.dumps(signal.log_phrase)}"
        start = occurred_at - timedelta(minutes=1)
        end = occurred_at + timedelta(minutes=3)
        data = self._json(
            self.settings.loki_url,
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": int(start.timestamp() * 1_000_000_000),
                "end": int(end.timestamp() * 1_000_000_000),
                "limit": 3,
            },
        )
        output: list[Evidence] = []
        for stream in data.get("data", {}).get("result", []):
            for timestamp, line in stream.get("values", []):
                output.append(
                    Evidence(
                        source_type="LOG",
                        source_ref=f"loki:{service}:{timestamp}",
                        kind="ERROR_LOG",
                        subject=signal.hypothesis_key,
                        polarity="SUPPORT",
                        observed_at=datetime.fromtimestamp(int(timestamp) / 1_000_000_000, UTC),
                        collected_at=datetime.now(UTC),
                        tool_name="loki.query_range",
                        tool_version=TOOL_VERSION,
                        payload={"message_excerpt": str(line)[:300], "service": service},
                    )
                )
                if len(output) >= 3:
                    return output
        return output

    def traces(
        self, signal: SignalDefinition, *, service: str, occurred_at: datetime
    ) -> list[Evidence]:
        start = occurred_at - timedelta(minutes=1)
        end = occurred_at + timedelta(minutes=3)
        data = self._json(
            self.settings.tempo_url,
            "/api/search",
            {
                "tags": f"service.name={service}",
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "limit": 8,
            },
        )
        output: list[Evidence] = []
        for item in data.get("traces", [])[:8]:
            trace_id = item.get("traceID", "")
            if not TRACE_ID.fullmatch(trace_id):
                continue
            trace = self._json(self.settings.tempo_url, f"/api/traces/{trace_id}", {})
            error_spans: list[str] = []
            for batch in trace.get("batches", []):
                service_name = next(
                    (
                        attribute.get("value", {}).get("stringValue")
                        for attribute in batch.get("resource", {}).get("attributes", [])
                        if attribute.get("key") == "service.name"
                    ),
                    None,
                )
                if service_name != service:
                    continue
                for scope in batch.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        name = span.get("name", "")
                        if span.get("status", {}).get("code") != "STATUS_CODE_ERROR":
                            continue
                        if signal.trace_span and name != signal.trace_span:
                            continue
                        error_spans.append(name)
            if not error_spans:
                continue
            output.append(
                Evidence(
                    source_type="TRACE",
                    source_ref=f"tempo:{trace_id}",
                    kind="ERROR_SPAN",
                    subject=signal.hypothesis_key,
                    polarity="SUPPORT",
                    observed_at=datetime.fromtimestamp(
                        int(item["startTimeUnixNano"]) / 1_000_000_000, UTC
                    ),
                    collected_at=datetime.now(UTC),
                    tool_name="tempo.search_and_get_trace",
                    tool_version=TOOL_VERSION,
                    payload={
                        "trace_id": trace_id,
                        "service": service,
                        "error_spans": error_spans[:5],
                    },
                )
            )
            if len(output) >= 3:
                break
        return output
