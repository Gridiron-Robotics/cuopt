# Gridiron integration overlay for NVIDIA cuOpt

This directory is **Gridiron-owned integration code** layered on top of the
upstream [NVIDIA cuOpt](https://github.com/NVIDIA/cuopt) repo. The upstream
sources (`cpp/`, `python/`, `cmake/`, …) are the GPU VRP/LP/MILP solver and are
**never modified** — that is the house rule. All of Gridiron's value lives here,
in the seams around the untouched solver:

| Path | Role |
|---|---|
| `gridiron/observability/` | OpenObserve/OTLP self-heal drop-in + an instrumented ASGI entrypoint that wraps the upstream cuOpt server so failed solves surface as estate incidents. |

## Where cuOpt sits in the estate

cuOpt is a **GPU-required Server API**, not an MCP module. It is reached through
its consuming module's client, not directly by the agent brain:

- `delivery_optimization` calls it via `CUOPT_BASE_URL` (`/cuopt/request`) for
  last-mile routing.
- The robotics **fleet VRP task-assignment hook** (which robot does which
  pick/place, and in what order) is *scoped* — it will encode the fleet
  assignment problem as a cuOpt request through the same seam. Not yet built.

Because cuOpt is upstream + GPU-bound, there is deliberately **no Contract-A MCP
surface in this repo** — that would be a fork of upstream. The MCP/tool surface
belongs to the consuming modules (`delivery_optimization`, the fleet gateway),
which already expose it.

See `gridiron/observability/README.md` for the observability seam.
