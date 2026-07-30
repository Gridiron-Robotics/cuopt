# Gridiron integration overlay for NVIDIA cuOpt

This directory is **Gridiron-owned integration code** layered on top of the
upstream [NVIDIA cuOpt](https://github.com/NVIDIA/cuopt) repo. The upstream
sources (`cpp/`, `python/`, `cmake/`, …) are the GPU VRP/LP/MILP solver and are
**never modified** — that is the house rule. All of Gridiron's value lives here,
in the seams around the untouched solver:

| Path | Role |
|---|---|
| `gridiron/observability/` | OpenObserve/OTLP self-heal drop-in + an instrumented ASGI entrypoint that wraps the upstream cuOpt server so failed solves surface as estate incidents. |
| `gridiron/mcp/` | The estate **Contract-A** tool surface (`GET /tools`, `POST /invoke`) over the solver, plus the fleet task-assignment encoder/decoder. Runs as a sidecar with no GPU, or mounts at `/mcp` on the co-located server. |

## Where cuOpt sits in the estate

cuOpt is a **GPU-required Server API**. Its consumers reach it two ways:

- **Directly, via its own client.** `delivery_optimization` calls
  `/cuopt/request` at `CUOPT_BASE_URL` for last-mile routing.
- **As agent tools, via `gridiron/mcp/`.** The robotics fleet hook — which robot
  does which pick/place, and in what order — is the `assign_fleet_tasks` tool.

### Note: this repo used to say it should have no MCP surface

An earlier version of this README argued that a Contract-A surface here would
amount to forking upstream, and that the tool surface belonged only to the
consuming modules. That was wrong on both counts, and the correction is the
`gridiron/mcp/` overlay:

1. The surface is **in the overlay**, not upstream. No cuOpt source is touched,
   and the sidecar runs without the cuOpt runtime or a GPU at all.
2. Pushing the surface into consumers meant each one re-derived cuOpt's
   **index-space** request model — vehicles and tasks as positions in a cost
   matrix — and an indexing mistake there produces a payload that VALIDATES and
   returns a confident answer about the wrong robots. Encoding it once, tested
   against upstream's own documented examples, is the point.

See `gridiron/observability/README.md` and `gridiron/mcp/README.md` for each seam.
