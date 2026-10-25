from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def configure_tracing(app: FastAPI, *, enabled: bool) -> None:
    if not enabled:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "aegisops-api"}))
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="/api/v1/health/live,/api/v1/health/ready"
    )
