# Gridiron estate deploy path — self-hosted cuOpt solver

This directory is the **Gridiron Robotics deploy bundle** for standing up the
self-hosted NVIDIA cuOpt solver (`cuopt-server`) that the **warehouse estate**
points at:

- **simulation** (GridSim warehouse DES) → `CUOPT_URL`
- **floorplans-to-USD-scenes** (capture pipeline) → `RL_CUOPT_REST_URL` (with `RL_CUOPT_BACKEND=rest`)

Both call the async routing contract `POST /cuopt/request` → `GET /cuopt/solution/{reqId}` on port `5000`.

> **Why it lives here (in the `cuopt` fork):** this deploys the *actual cuOpt
> solver* the warehouse estate uses for intralogistics VRP (AGV routing around
> racks/walls). It is **not** related to `cuopt_routes_solver`, which is a
> separate over-the-road tire-delivery product. Deploy tooling for "our
> cuopt-server" belongs with the solver, i.e. this repo.

## Contents

| Path | Purpose |
|---|---|
| `helmchart/cuopt-server/` | Helm v3 chart (Gridiron conventions: labels, naming, pinned image, no secrets) that runs `cuopt-server` as a standalone GPU microservice, plus the `cuopt-mcp` sidecar Deployment/Service. |
| `docker-compose.cuopt.yml` | The same two containers for a single-GPU dev workstation. |
| `Dockerfile.mcp` | Image for the **`cuopt-mcp` sidecar** — the Contract-A tool surface (`gridiron.mcp.app:app`) on port **8090**. No GPU. |
| `Dockerfile.mcp.dockerignore` | Keeps that image's build context to `gridiron/` instead of the whole cuOpt source tree. |
| `tests/test_helmchart.py` | Offline structural gate (image tags, ports 5000/8090, `nvidia.com/gpu`, solver command, shared network, template presence). |

## The two services

| Service | Port | GPU | What it is |
|---|---|---|---|
| `cuopt` | 5000 | **yes**, one whole GPU per replica | The upstream NVIDIA solver. Async REST: `POST /cuopt/request` → `GET /cuopt/solution/{reqId}`. |
| `cuopt-mcp` | 8090 | no | The estate Contract-A tool surface (`GET /tools`, `POST /invoke`, `HEAD /`). Proxies to `cuopt` over HTTP. |

They are separate containers on purpose: the solver pins a GPU per replica, so
the tool surface must scale — and be addressed — independently. The names are
load-bearing; the brain already holds `http://cuopt-mcp:8090`.

> The upstream NVIDIA chart also ships at [`../helmchart/cuopt-server`](../helmchart/cuopt-server);
> this Gridiron chart is the estate-conventions equivalent (functionally the same
> image/port/GPU/probes). Use whichever your workflow prefers — the estate wiring
> below is identical.

## Deploy (Kubernetes)

```bash
# From this directory; needs a GPU node with nvidia.com/gpu schedulable.
# The MCP sidecar's bearer token comes from a Secret that already exists —
# no secret is templated by this chart.
kubectl create secret generic cuopt-mcp-secret --from-literal=CUOPT_MCP_TOKEN=<estate service token>

helm install cuopt-server ./helmchart/cuopt-server \
  --set mcp.tokenSecret.name=cuopt-mcp-secret

# Verify the solver
kubectl port-forward svc/cuopt-server 5000:5000
curl http://localhost:5000/cuopt/health

# Verify the tool surface (Service is named literally `cuopt-mcp`)
kubectl port-forward svc/cuopt-mcp 8090:8090
curl -I http://localhost:8090/                                   # 200, unauthenticated
curl -H "Authorization: Bearer <token>" http://localhost:8090/tools?server=cuopt
```

Leave `mcp.tokenSecret.name` empty and the sidecar still deploys — but it
**fail-closes**: every tool route answers 503 rather than serving unmetered GPU
time without a credential. Set `mcp.enabled=false` to deploy the solver alone.

## Deploy (single-GPU workstation)

```bash
# The shared estate network is created once (by erp_django_middleware); joined here.
docker network create erp_shared_network 2>/dev/null || true

# CUOPT_MCP_TOKEN is required — `up` fails fast rather than booting a surface
# that 503s on every call while looking alive.
CUOPT_MCP_TOKEN=<estate service token> docker compose -f docker-compose.cuopt.yml up -d

curl http://localhost:5000/cuopt/health
curl -I http://localhost:8090/
curl -H "Authorization: Bearer <token>" http://localhost:8090/tools?server=cuopt
```

Building `cuopt-mcp` uses the **repo root** as its build context (the image needs
the `gridiron/` package); `Dockerfile.mcp.dockerignore` trims that context to the
overlay.

## Wire the estate services

```bash
# simulation
CUOPT_URL=http://cuopt-server:5000
# floorplans
RL_CUOPT_REST_URL=http://cuopt-server:5000
RL_CUOPT_BACKEND=rest
# langgraph-agents (the brain) — Contract-A tool surface, bearer CUOPT_MCP_TOKEN
gpu_solver -> http://cuopt-mcp:8090   (kind: contract, server: cuopt)
```

## Self-heal rail

Both services ship traces + ERROR logs to OpenObserve on the **`cuopt`** stream
(`OTEL_SERVICE_NAME=cuopt`), which is also the `module` on the incident the
langgraph self-heal loop receives. That one string is defined once, in
`gridiron/observability/gridiron_otel.py` (`DEFAULT_SERVICE_NAME`), and
`gridiron/verify.sh` fails if the stream, the MCP server name and the incident
module ever disagree. With `OTEL_EXPORTER_OTLP_ENDPOINT` unset the drop-in is a
graceful no-op, so a workstation run needs no OpenObserve.

Image: `nvidia/cuopt:26.8.0-cuda12.9-py3.12` (Docker Hub, pinned). Requires an
NVIDIA GPU node + the device plugin; each replica consumes one full GPU.
