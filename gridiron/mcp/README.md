<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Gridiron MCP overlay for cuOpt

The estate **Contract-A** tool surface over the NVIDIA cuOpt solver. Upstream
cuOpt is unmodified (house rule) — everything here is the integration seam.

```
GET  /tools[?server=cuopt]   -> {"server":"cuopt","tools":[…]}
POST /invoke                 -> {"tool":…,"result":…[, "replayed":true]}
HEAD /                       -> 200 (open, so a gateway can probe liveness)
```

## Tools

| Tool | destructive | What it does |
|---|---|---|
| `assign_fleet_tasks` | no | Robots + a worklist → which robot does which task, in what order |
| `submit_cuopt_problem` | no | A native VRP/LP/MILP body → a request id |
| `get_solve_status` | no | Poll a submitted request |
| `get_solve_result` | no | Fetch the solution (routing results also decoded) |
| `cancel_solve` | **yes** | Delete a queued/cached request |
| `solver_health` | no | Is the GPU solver reachable |

`assign_fleet_tasks` is the tool that matters. cuOpt's request body is an
**index-space** model: vehicles and tasks are positions in a cost matrix, and the
answer comes back keyed by those positions. An agent asked to author that body
directly will get the indexing wrong in a way that still *validates* — the worst
available failure, because the result looks authoritative and describes the wrong
robots. So the encoding lives in `fleet_assignment.py` and is unit-tested against
upstream's own documented request/response examples.

`submit_cuopt_problem` remains as the escape hatch for a caller that already
speaks cuOpt natively.

### `destructiveHint`

Only `cancel_solve` is destructive. Solving is expensive but changes nothing a
human would need to approve; deleting a request destroys work and cached input,
so it goes through the middleware HITL approval gate.

## Running it

**Sidecar** (no GPU — the recommended shape; the GPU box stays a pure solver):

```bash
pip install -r gridiron/mcp/requirements.txt
CUOPT_BASE_URL=http://cuopt-solver:5000 \
CUOPT_MCP_TOKEN=<estate service token> \
uvicorn gridiron.mcp.app:app --host 0.0.0.0 --port 5100
```

**Co-located** — `gridiron.observability.asgi` mounts the same router at `/mcp`
on the upstream app, so one port serves both. Do *not* install
`gridiron/mcp/requirements.txt` there: the cuOpt server already brings fastapi
and pins `uvicorn==0.34.*` itself.

## Auth is fail-closed

With neither `CUOPT_MCP_TOKEN` set nor `CUOPT_MCP_ALLOW_INSECURE=true`, every
tool route answers **503** and refuses to serve. An open tool surface on a solver
is not only a data-exposure problem — it is unmetered GPU time for anyone who can
reach the port. `HEAD /` stays open regardless; it leaks nothing and a gateway
needs it.

| env | effect |
|---|---|
| `CUOPT_MCP_TOKEN` | the bearer token compared with `hmac.compare_digest` |
| `CUOPT_MCP_ALLOW_INSECURE` | `true` reopens the surface — local development only |
| `CUOPT_BASE_URL` | the upstream solver (default `http://localhost:5000`) |
| `CUOPT_TIMEOUT_SECONDS` | per-call HTTP timeout (default 30) |

## Idempotency

`Idempotency-Key` replays a prior result instead of re-solving, keyed by
`(tenant, tool, key)` so one tenant's cached solution can never answer another's
call. The cache is a bounded in-process LRU, and therefore **per worker**: a retry
landing on a different worker re-solves. That is a cost, not a correctness bug —
every tool here is idempotent by construction (solving twice yields another
solution; deleting twice is still deleted). A module whose tools mutated business
state would need Redis instead.

## Errors

Non-2xx JSON `{"error": …}`, never an unhandled 500: `400` bad model or body,
`401` missing bearer, `403` wrong bearer, `404` unknown server/tool, `422`
missing required argument (checked **before** the solver is called), `502` solver
unreachable or refusing, `503` fail-closed.

A solver fault logs at **ERROR** so the OpenObserve `level=error` alert fires the
langgraph self-heal loop. That is deliberate: Contract A converts failures into
structured non-2xx responses, so there is no unhandled 5xx for the rail to key
off, and a rail hung off a transport exception would report nothing while the
solver is down. Caller mistakes (a bad location index) stay at WARNING — logging
them at ERROR would raise an incident for every malformed agent call and bury the
real faults.

## Tests

```bash
python -m pytest gridiron/ -q     # no GPU, no solver: the transport is injected
```

`test_fleet_assignment.py` covers the index translation both ways (including the
cases that produce a plausible wrong answer rather than an error);
`test_mcp_contract.py` covers the catalog shape, fail-closed auth, dispatch,
error mapping, and idempotency.
