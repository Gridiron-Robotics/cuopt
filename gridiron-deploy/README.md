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
| `helmchart/cuopt-server/` | Helm v3 chart (Gridiron conventions: labels, naming, pinned image, no secrets) that runs `cuopt-server` as a standalone GPU microservice. |
| `docker-compose.cuopt.yml` | The same container for a single-GPU dev workstation. |
| `tests/test_helmchart.py` | Offline structural gate (image tag, port 5000, `nvidia.com/gpu`, solver command, template presence). |

> The upstream NVIDIA chart also ships at [`../helmchart/cuopt-server`](../helmchart/cuopt-server);
> this Gridiron chart is the estate-conventions equivalent (functionally the same
> image/port/GPU/probes). Use whichever your workflow prefers — the estate wiring
> below is identical.

## Deploy (Kubernetes)

```bash
# From this directory; needs a GPU node with nvidia.com/gpu schedulable.
helm install cuopt-server ./helmchart/cuopt-server

# Verify
kubectl port-forward svc/cuopt-server 5000:5000
curl http://localhost:5000/cuopt/health
```

## Deploy (single-GPU workstation)

```bash
docker compose -f docker-compose.cuopt.yml up -d
curl http://localhost:5000/cuopt/health
```

## Wire the estate services

```bash
# simulation
CUOPT_URL=http://cuopt-server:5000
# floorplans
RL_CUOPT_REST_URL=http://cuopt-server:5000
RL_CUOPT_BACKEND=rest
```

Image: `nvidia/cuopt:26.8.0-cuda12.9-py3.12` (Docker Hub, pinned). Requires an
NVIDIA GPU node + the device plugin; each replica consumes one full GPU.
