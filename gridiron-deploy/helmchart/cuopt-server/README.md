# cuopt-server Helm chart

Gridiron estate deploy path for the **self-hosted NVIDIA cuOpt solver**
(`cuopt-server`). It stands up the real solver as a standalone GPU microservice
inside Kubernetes so the estate's services can call a live optimizer instead of a
mock backend:

- **Simulation service** → `CUOPT_URL`
- **Floorplans pipeline** → `RL_CUOPT_REST_URL` (with `RL_CUOPT_BACKEND=rest`)

Both expect `POST http://<host>:5000/cuopt/request`. This chart provides that host.

The chart mirrors the upstream NVIDIA cuOpt Server chart but is re-homed to
estate conventions (labels, naming, pinned image tag, no secrets).

## What it deploys

| Object | Purpose |
|---|---|
| `Deployment` | One cuOpt solver pod (`nvidia/cuopt`), one GPU each, liveness/readiness on `/v2/health/{live,ready}` |
| `Service` (ClusterIP) | Stable in-cluster endpoint on port `5000` (named `http`) |
| `ServiceAccount` | Dedicated identity for the pods (toggle with `serviceAccount.create`) |
| `Ingress` | Optional external exposure (disabled by default) |

Image: **`nvidia/cuopt:26.8.0-cuda12.9-py3.12`** (Docker Hub — pinned, not `latest`).
Container command: `python -m cuopt_server.cuopt_service -p 5000`.

## Prerequisites

- A Kubernetes cluster with at least one **GPU node** where `nvidia.com/gpu` is
  schedulable — i.e. the **NVIDIA device plugin** (or the NVIDIA GPU Operator) is
  installed and the node advertises GPUs.
- Helm **3.x**.
- Network egress to Docker Hub (or a mirror configured via `image.repository` +
  `imagePullSecrets`) to pull the ~multi-GB cuOpt image.

If your GPU nodes carry a taint (e.g. `nvidia.com/gpu=present:NoSchedule`), add a
matching toleration under `tolerations` in `values.yaml` (a commented example is
included). On clusters that schedule GPUs via a `RuntimeClass`, set
`gpu.runtimeClassName` (e.g. `nvidia`).

## Install

```bash
# From the repo root:
helm install cuopt-server ./helmchart/cuopt-server

# Or with overrides:
helm install cuopt-server ./helmchart/cuopt-server \
  --set replicaCount=1 \
  --set image.tag=26.8.0-cuda12.9-py3.12
```

Watch it come up (model load on cold start is slow):

```bash
kubectl get pods -l app.kubernetes.io/name=cuopt-server -w
```

## Verify

```bash
# Port-forward the ClusterIP service to your workstation:
kubectl port-forward svc/cuopt-server 5000:5000

# cuOpt health endpoint should return HTTP 200:
curl http://localhost:5000/cuopt/health
```

## Wire the estate services

Point the estate services at the in-cluster service DNS name
(`http://<release>-cuopt-server:5000`, or `http://cuopt-server:5000` when the
release name already contains the chart name, as above):

```bash
# Simulation service (gridiron/simulation)
CUOPT_URL=http://cuopt-server:5000

# Floorplans pipeline (floorplans-to-USD-scenes)
RL_CUOPT_REST_URL=http://cuopt-server:5000
RL_CUOPT_BACKEND=rest
```

## Request/response contract (cuOpt 26.08)

`cuopt-server` is asynchronous:

1. **Submit** a routing/optimization problem:
   `POST http://<service>:5000/cuopt/request` → returns a `reqId`.
2. **Poll** for the result:
   `GET http://<service>:5000/cuopt/solution/{reqId}` until the solution is ready.

Health: `GET /cuopt/health`. Kubernetes probes use the server's own
`/v2/health/live` and `/v2/health/ready` endpoints.

## Configuration

See `values.yaml` for the full, commented list. Common knobs:

| Key | Default | Notes |
|---|---|---|
| `replicaCount` | `1` | Each replica pins one full GPU. |
| `image.repository` | `nvidia/cuopt` | Docker Hub image. |
| `image.tag` | `26.8.0-cuda12.9-py3.12` | Exact pinned tag. |
| `service.type` | `ClusterIP` | Internal-only by default. |
| `service.port` / `service.targetPort` | `5000` | cuOpt listens on 5000. |
| `resources.requests`/`limits` `nvidia.com/gpu` | `1` | GPUs are non-overcommittable. |
| `ingress.enabled` | `false` | cuOpt has no built-in auth — front it if you expose it. |
| `serviceAccount.create` | `true` | Dedicated pod identity. |
| `autoscaling.enabled` | `false` | Scale deliberately against real GPU capacity. |
| `tolerations` | `[]` | Add a toleration for tainted GPU node pools. |
| `gpu.runtimeClassName` | `""` | Set to `nvidia` on RuntimeClass-based clusters. |

## Uninstall

```bash
helm uninstall cuopt-server
```

## Local (non-Kubernetes) alternative

For a single developer workstation with a GPU, the repo also ships
`docker-compose.cuopt.yml` at the repo root, which runs the same
`nvidia/cuopt:26.8.0-cuda12.9-py3.12` container locally on port 5000.
