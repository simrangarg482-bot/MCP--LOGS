"""Tests for `app.shared.config.tracing.configure_tracing` (Phase 5.3).

`app.api.main`'s own module-level `app = create_app()` already exercises
this for real on every test collection (`configure_tracing` runs
unconditionally in `create_app`) -- these tests pin down the specific
behavior more directly rather than relying on that indirect signal alone.
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.shared.config.tracing import configure_tracing


def test_configure_tracing_sets_a_real_tracer_provider() -> None:
    app = FastAPI()

    configure_tracing(app)

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_configure_tracing_instruments_the_app_without_raising() -> None:
    """The real regression this guards against: `FastAPIInstrumentor.
    instrument_app` raising (e.g. a version mismatch between `fastapi` and
    `opentelemetry-instrumentation-fastapi`) would otherwise only surface as
    a startup crash the first time `create_app()` runs for real.
    """
    app = FastAPI()

    configure_tracing(app)  # must not raise

    assert app is not None
