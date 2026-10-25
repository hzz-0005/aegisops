import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, Request
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.status import Status, StatusCode


@contextmanager
def database() -> Iterator[psycopg.Connection]:
    with psycopg.connect(os.environ["LAB_DATABASE_URL"], connect_timeout=2) as connection:
        yield connection


def cgroup_oom_kills() -> int | None:
    """Read a generic cgroup v2 OOM event, including killed child processes."""
    try:
        events = Path("/sys/fs/cgroup/memory.events").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in events.splitlines():
        name, _, value = line.partition(" ")
        if name == "oom_kill":
            return int(value)
    return None


def _publish_request_failure(payload: dict) -> None:
    url = os.getenv("LAB_REQUEST_EVENT_URL")
    token = os.getenv("LAB_REQUEST_EVENT_TOKEN")
    if not url or not token:
        return
    try:
        with httpx.Client(timeout=1.5, trust_env=False) as client:
            response = client.post(
                url, json=payload, headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logging.getLogger("lab.request_events").warning(
            "request failure event delivery failed: %s", type(exc).__name__
        )


def mark_request_failure(
    request: Request, *, service: str, failure_code: str, resource: str
) -> None:
    """Bind a failure fact to the request; middleware delivers it after error responses."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid or not span.is_recording():
        logging.getLogger("lab.request_events").warning("request span is unavailable")
        return
    span.set_attribute("aegisops.failure_code", failure_code)
    span.set_attribute("aegisops.resource", resource)
    span.set_status(Status(StatusCode.ERROR))
    request.state.failure_event = {
        "schema_version": 1,
        "source_event_id": str(uuid4()),
        "service": service,
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "failure_code": failure_code,
        "resource": resource,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def create_app(service_name: str) -> FastAPI:
    app = FastAPI(title=f"AegisOps Lab {service_name}")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    lab_logger = logging.getLogger(f"lab.{service_name}")
    lab_logger.setLevel(logging.INFO)
    lab_logger.propagate = False
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    lab_logger.addHandler(console)

    if endpoint:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "aegisops-lab",
                "service.instance.id": service_name,
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
            export_interval_millis=3000,
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
        )
        set_logger_provider(logger_provider)
        lab_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
        FastAPIInstrumentor.instrument_app(app, excluded_urls="/health/live,/health/ready")

    request_counter = metrics.get_meter("aegisops.lab").create_counter(
        "lab_http_requests", description="Lab HTTP requests by service and status"
    )

    def observe_oom_kills(_options: CallbackOptions) -> list[Observation]:
        value = cgroup_oom_kills()
        return [Observation(value, {"service": service_name})] if value is not None else []

    metrics.get_meter("aegisops.lab").create_observable_counter(
        "lab_container_oom_kills",
        callbacks=[observe_oom_kills],
        description="Container cgroup OOM kills, including child processes",
    )

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            request_counter.add(1, {"service": service_name, "status": "500"})
            raise
        request_counter.add(1, {"service": service_name, "status": str(response.status_code)})
        if payload := getattr(request.state, "failure_event", None):
            tasks = BackgroundTasks()
            if response.background is not None:
                tasks.add_task(response.background)
            tasks.add_task(_publish_request_failure, payload)
            response.background = tasks
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    return app
