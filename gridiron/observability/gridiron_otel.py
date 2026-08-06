# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""gridiron_otel — estate OpenObserve/OTLP self-heal drop-in (single file).

Copy this ONE file into any Python service and call ``setup_observability()``
once at startup. It ships the service's **traces + logs (especially ERRORS)**
to OpenObserve via OTLP/HTTP. Because the estate alert rule fires on any
``level=error`` record in the module's stream, an unhandled error here becomes
an OpenObserve alert → the langgraph self-heal loop
(``/webhooks/openobserve/alert`` → diagnose → fix→PR / runtime remediation).

It is **env-gated and fully graceful**: with the OTEL/OpenObserve env unset, or
the OpenTelemetry SDK / OTLP exporter not installed, every call no-ops and never
raises. Import it unconditionally; it costs nothing when disabled.

Contract (see ``deploy/observability/README.md``):

    OTEL_ENABLED=1                         # master switch (also on if endpoint is set)
    OTEL_EXPORTER_OTLP_ENDPOINT=http://openobserve:5080/api/<org>/v1
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 email:password>
    OTEL_SERVICE_NAME=<module>             # == the OpenObserve stream == incident 'module'
    OTEL_LOG_LEVEL=ERROR                   # min level shipped as OTLP logs (default ERROR)

Usage — FastAPI/Starlette:

    from gridiron_otel import setup_observability
    app = FastAPI()
    setup_observability("placement", app=app)   # traces + error-logs + traceparent

Usage — any Python process (Django, worker, CLI):

    from gridiron_otel import setup_observability
    setup_observability("erp-middleware")

Cross-service propagation (MCP / NATS), so one incident keeps its trace id:

    from gridiron_otel import traceparent_headers
    headers.update(traceparent_headers())

Requires (pin these exact-or-newer in the module's manifest):
    opentelemetry-api  opentelemetry-sdk  opentelemetry-exporter-otlp-proto-http
    opentelemetry-instrumentation-fastapi  opentelemetry-instrumentation-httpx
"""

from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger("gridiron_otel")

# Idempotency guard — setup runs once per process.
_STATE: dict[str, Any] = {"done": False, "status": None, "providers": []}


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """True when observability should initialize (master switch or endpoint set)."""
    return _truthy(os.environ.get("OTEL_ENABLED")) or bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )


def _resolve_level(level: int | str | None) -> int:
    if isinstance(level, int):
        return level
    name = (
        (level or os.environ.get("OTEL_LOG_LEVEL") or "ERROR").strip().upper()
    )
    return getattr(logging, name, logging.ERROR)


def setup_observability(
    service_name: str | None = None,
    *,
    app: Any = None,
    level: int | str | None = None,
    _span_exporter: Any = None,
    _log_exporter: Any = None,
) -> dict[str, bool]:
    """Initialise OTLP traces + error-logs export to OpenObserve. Idempotent.

    ``service_name`` becomes the OTLP ``service.name`` = the OpenObserve stream =
    the incident's ``module``; falls back to ``OTEL_SERVICE_NAME`` then the
    process name. ``app`` (FastAPI/Starlette) is auto-instrumented when the
    instrumentation package is present. ``_span_exporter`` / ``_log_exporter``
    are test hooks (inject in-memory exporters); production leaves them None so
    the OTLP/HTTP exporters are used.

    Returns a status dict; on any failure it degrades and returns what came up.
    """
    if _STATE["done"]:
        return _STATE["status"]

    status = {"traces": False, "logs": False, "fastapi": False, "httpx": False}
    forced = _span_exporter is not None or _log_exporter is not None
    if not enabled() and not forced:
        _STATE.update(done=True, status=status)
        return status

    name = (
        service_name
        or os.environ.get("OTEL_SERVICE_NAME")
        or os.path.basename(os.environ.get("_", "")).strip()
        or "unknown-service"
    )

    try:
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": name})
    except Exception as exc:  # SDK missing — full graceful no-op
        _log.info(
            "gridiron_otel: SDK unavailable, observability disabled (%s)", exc
        )
        _STATE.update(done=True, status=status)
        return status

    # ── traces ──────────────────────────────────────────────────────────────
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )

        span_exp = _span_exporter
        if span_exp is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            span_exp = OTLPSpanExporter()
        tp = TracerProvider(resource=resource)
        tp.add_span_processor(
            SimpleSpanProcessor(span_exp)
            if forced
            else BatchSpanProcessor(span_exp)
        )
        trace.set_tracer_provider(tp)
        _STATE["providers"].append(tp)
        status["traces"] = True
    except Exception as exc:
        _log.info("gridiron_otel: trace export unavailable (%s)", exc)

    # ── logs (the self-heal trigger: ERROR records → level=error → alert) ─────
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor,
            SimpleLogRecordProcessor,
        )

        log_exp = _log_exporter
        if log_exp is None:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter,
            )

            log_exp = OTLPLogExporter()
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(
            SimpleLogRecordProcessor(log_exp)
            if forced
            else BatchLogRecordProcessor(log_exp)
        )
        set_logger_provider(lp)
        handler = LoggingHandler(
            level=_resolve_level(level), logger_provider=lp
        )
        logging.getLogger().addHandler(handler)
        _STATE["providers"].append(lp)
        status["logs"] = True
    except Exception as exc:
        _log.info("gridiron_otel: log export unavailable (%s)", exc)

    # ── optional app + httpx instrumentation (traceparent propagation) ────────
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import (
                FastAPIInstrumentor,
            )

            FastAPIInstrumentor.instrument_app(app)
            status["fastapi"] = True
        except Exception as exc:
            _log.info(
                "gridiron_otel: FastAPI instrumentation skipped (%s)", exc
            )
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        status["httpx"] = True
    except Exception as exc:
        _log.info("gridiron_otel: httpx instrumentation skipped (%s)", exc)

    _STATE.update(done=True, status=status)
    _log.info("gridiron_otel: observability up for %r → %s", name, status)
    return status


def traceparent_headers(
    carrier: dict[str, str] | None = None,
) -> dict[str, str]:
    """Inject the current trace context as W3C ``traceparent`` headers.

    Use on every outbound MCP/NATS call so a downstream error shares this
    request's trace id — one incident, one thread. No-op (returns the carrier
    unchanged) when tracing isn't configured.
    """
    carrier = {} if carrier is None else carrier
    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    except Exception:
        pass
    return carrier


def shutdown() -> None:
    """Flush + shut down providers (clean process exit / test teardown)."""
    import contextlib

    for p in _STATE["providers"]:
        for m in ("force_flush", "shutdown"):
            with contextlib.suppress(Exception):
                getattr(p, m)()
    _STATE.update(done=False, status=None, providers=[])
