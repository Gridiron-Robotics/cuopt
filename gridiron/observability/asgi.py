# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""gridiron/observability/asgi.py — instrumented ASGI entrypoint for cuOpt server.

The NVIDIA cuOpt server (``cuopt_server.webserver:app``) is **upstream and
unmodified** (house rule). This wrapper is the Gridiron *integration seam*: it
imports the upstream FastAPI ``app`` and calls the estate self-heal drop-in
(``gridiron_otel.setup_observability``) so the solver's **traces + ERROR logs**
flow to OpenObserve. A failed / crashing solve then raises the estate
``level=error`` alert on the ``cuopt`` stream → the langgraph self-heal loop
diagnoses and repairs it — without editing a single line of upstream cuOpt.

Deployment points uvicorn at THIS module instead of the upstream one:

    # was:  uvicorn cuopt_server.webserver:app --host 0.0.0.0 --port 5000
    uvicorn gridiron.observability.asgi:app --host 0.0.0.0 --port 5000

Env contract (see gridiron/observability/README.md and the estate drop-in):
    OTEL_ENABLED=1
    OTEL_EXPORTER_OTLP_ENDPOINT=http://openobserve:5080/api/<org>/v1
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 email:password>
    OTEL_SERVICE_NAME=cuopt          # == the OpenObserve stream == incident 'module'
    OTEL_LOG_LEVEL=ERROR

With the OTEL_* env unset (or the OpenTelemetry SDK absent) the wrapper is a
transparent pass-through: it returns the untouched upstream app.
"""

from __future__ import annotations

import logging

# Import the estate drop-in from the same overlay package. Keep this file free of
# any *upstream* import at module top so tooling that only inspects the overlay
# (lint/type/test) never needs cuOpt or a GPU present.
from gridiron.observability.gridiron_otel import setup_observability

# The OpenObserve stream / incident module for this service.
SERVICE_NAME = "cuopt"


def build_app():
    """Import the upstream cuOpt FastAPI app and instrument it in place.

    Importing ``cuopt_server.webserver`` pulls in the full cuOpt runtime (and,
    in production, GPU libraries) — so it is imported lazily *here*, only when an
    ASGI server actually boots this entrypoint, never at overlay import time.

    The Contract-A MCP router is mounted on the same app, so a co-located deploy
    serves the solver and its tool surface on one port. Mounting is best-effort:
    the solver must still boot if the MCP overlay's own deps are missing.
    """
    from cuopt_server.webserver import app as upstream_app

    # Instrument in place: OTLP traces + ERROR-log export + FastAPI/httpx spans.
    # setup_observability is idempotent and a graceful no-op when OTEL_* is unset.
    setup_observability(SERVICE_NAME, app=upstream_app)

    try:
        from gridiron.mcp.app import build_mcp_router

        upstream_app.include_router(build_mcp_router(), prefix="/mcp")
    except Exception:  # pragma: no cover - never block the solver on the overlay
        logging.getLogger(__name__).warning(
            "Gridiron MCP router not mounted; cuOpt serving without a tool surface",
            exc_info=True,
        )
    return upstream_app


def __getattr__(name: str):
    """Resolve ``app`` on first access (PEP 562).

    uvicorn's ``module:app`` target does a ``getattr`` after import, so this still
    works as an entrypoint — but merely *importing* this module (lint, type-check,
    a test that only wants SERVICE_NAME) no longer drags in the cuOpt runtime and
    a GPU. Building at module scope made the lazy import in ``build_app`` pointless.
    """
    if name == "app":
        return build_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
