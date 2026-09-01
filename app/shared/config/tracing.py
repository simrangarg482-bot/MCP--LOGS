"""OpenTelemetry tracing configuration (Phase 5.3).

Owned by: shared/ (cross-cutting, no business meaning of its own -- same
category as `logging.py`).

`opentelemetry-sdk`/`opentelemetry-instrumentation-fastapi` have been
dependencies of this project since before this phase (`pyproject.toml`) but
were never imported or initialized anywhere in `app/` until now -- this
module is what actually wires them up.

Deliberately minimal: `FastAPIInstrumentor.instrument_app` auto-creates one
span per HTTP request (method, route, status code) with zero manual
instrumentation needed anywhere else in the codebase -- this alone already
satisfies "trace API request" from Phase 5.3's list. Deeper per-stage spans
(dense retrieval, lexical retrieval, reranking, confidence evaluation) are
NOT added in this pass: `opentelemetry-instrumentation-fastapi` only
instruments the HTTP boundary, and hand-instrumenting every retrieval/agent
stage with `tracer.start_as_current_span(...)` calls is real, separate work
this module doesn't attempt to fake by claiming here.

No real trace backend (Jaeger/Tempo/Azure Monitor/etc.) is deployed
anywhere for this project yet -- see `docs/operations/observability.md`.
Exporting is therefore configuration-gated: set `OTEL_EXPORTER_OTLP_ENDPOINT`
once a real collector exists; until then, spans are created (so the
instrumentation itself is exercised and won't silently rot unused) but
never exported anywhere in production. Console export is independently
opt-in because printing a complete JSON span per ingested document can
materially slow a local full sync.
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from app.shared.config.settings import get_settings


def _build_provider(service_name: str) -> TracerProvider:
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )
    elif settings.otel_console_exporter_enabled:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    return provider


def configure_worker_tracing(service_name: str) -> None:
    """Configure tracing for a non-HTTP worker process."""
    trace.set_tracer_provider(_build_provider(service_name))


def configure_tracing(app: FastAPI) -> None:
    """Set up a `TracerProvider` and instrument `app`. Call once, at process
    startup, after `configure_logging()`.

    Safe to call in every environment (test/development/production) --
    unlike `configure_logging`, there's no meaningful "safe to call twice"
    concern here since this project only ever calls it once, from
    `app.api.main.create_app`.
    """
    provider = _build_provider("ekip-api")
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
