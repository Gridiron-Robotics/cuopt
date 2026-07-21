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
    """
    from cuopt_server.webserver import app as upstream_app

    # Instrument in place: OTLP traces + ERROR-log export + FastAPI/httpx spans.
    # setup_observability is idempotent and a graceful no-op when OTEL_* is unset.
    setup_observability(SERVICE_NAME, app=upstream_app)
    return upstream_app


# Module-level ASGI callable uvicorn imports as ``gridiron.observability.asgi:app``.
app = build_app()
