# cuOpt OpenObserve/OTLP self-heal overlay

Makes a **failed or crashing GPU solve self-heal**: the cuOpt server's traces
and **ERROR logs** flow to OpenObserve on the `cuopt` stream; the estate
`level=error` alert on that stream fires the langgraph self-heal webhook, which
diagnoses the failure and opens a fix — all **without editing upstream cuOpt**.

## Files

| File | Purpose |
|---|---|
| `gridiron_otel.py` | The estate self-heal drop-in (copied verbatim from `langgraph-agents/deploy/observability/dropin/`). Ships OTLP traces + ERROR logs to OpenObserve; graceful no-op when `OTEL_*` is unset. |
| `asgi.py` | Instrumented ASGI entrypoint. Imports the **unmodified** upstream `cuopt_server.webserver:app` and calls `setup_observability("cuopt", app=app)`. |
| `requirements.txt` | Pinned OTel deps (exact versions, estate rule). |
| `tests/test_gridiron_otel.py` | Proves a raised ERROR emits an ERROR-severity OTLP record on the `cuopt` stream, lower levels don't ship, and it no-ops when disabled. |

## Run cuOpt behind the wrapper

Install the extra deps into the cuOpt server image/env, then point the ASGI
server at **this** entrypoint instead of the upstream one — zero upstream edits:

```bash
pip install -r gridiron/observability/requirements.txt

# was:  uvicorn cuopt_server.webserver:app --host 0.0.0.0 --port 5000
uvicorn gridiron.observability.asgi:app --host 0.0.0.0 --port 5000
```

Env contract (identical across the estate; unset ⇒ transparent pass-through):

```bash
OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://openobserve:5080/api/<org>/v1
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 email:password>
OTEL_SERVICE_NAME=cuopt          # == the OpenObserve stream == incident 'module'
OTEL_LOG_LEVEL=ERROR
```

## Register the self-heal alert

Once the `cuopt` stream is receiving records, register the per-stream
`level=error` alert (idempotent) so errors reach the langgraph self-heal loop:

```bash
# from langgraph-agents/deploy/observability
OPENOBSERVE_URL=... OPENOBSERVE_ORG=... OPENOBSERVE_AUTH=... \
OPENOBSERVE_WEBHOOK_TOKEN=... ./apply-alerts.sh cuopt
```

## Test

```bash
python -m pytest gridiron/observability/tests/ -q
```

The tests import only the overlay drop-in — no GPU, no cuOpt runtime required.
