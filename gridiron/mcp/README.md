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
| `CUOPT_MCP_ENFORCE_REQUEST_OWNER` | `true` refuses poll/cancel of a solve you did not create (default **false**) |
| `CUOPT_BASE_URL` | the upstream solver (default `http://localhost:5000`) |
| `CUOPT_TIMEOUT_SECONDS` | per-call HTTP timeout (default 30) |

## Request ownership

Upstream cuOpt answers `/cuopt/request/{id}` on the id alone — it attaches no
owner to a `reqId`. So the overlay keeps one: `gridiron/mcp/ownership.py` records
who created every minted request id and refuses `get_solve_status`,
`get_solve_result` and `cancel_solve` to anyone else. Cancelling matters most —
it drops the request and its cached input, and the real caller's next poll 404s.

**What the owner is.** The *principal* — the pair `(sha256(bearer)[:16],
X-Tenant-Id)`, the only two identity-bearing inputs this transport receives. The
credential half is authenticated but, with one shared deployment token, constant;
the tenant half separates tenants but is caller-asserted. Neither alone is right:
the credential alone makes every tenant behind one gateway a co-owner, the header
alone looks like an authorization boundary while being a naming convention. The
pair degrades in the right direction — worth exactly what the header is worth
today, and a real boundary the day the estate issues one credential per tenant,
with no code change here.

**Default-off, and why.** `CUOPT_MCP_ENFORCE_REQUEST_OWNER` ships `false`.

* With it on, this contains what this surface actually sees: an agent or gateway
  carrying a `reqId` from one tenant's context into another's, and a destructive
  cross-tenant `cancel_solve` by mistake. The check runs **before** the solver is
  contacted and before the idempotency replay shortcut.
* It does **not** contain a hostile holder of the shared token, who can assert
  the victim's `X-Tenant-Id` and present a matching principal. That is not
  claimable without a per-tenant credential — estate token-issuance work, not
  overlay work.

**Two preconditions before turning it on.** The owner map is an in-process
bounded LRU, so (1) run one worker, pin sessions, or replace `OwnerRegistry` with
a shared store — an id recorded on another worker is refused, not waved through,
because fail-open would make the check bypassable by load-balancer luck; and
(2) `MAX_OWNER_ENTRIES` (4096) records are kept, so a solve polled after 4096
newer ones ages out and is refused. Both failure modes are loud by design.

## Idempotency

`Idempotency-Key` replays a prior result instead of re-solving, keyed by
`(principal, tool, key)` — the same principal that owns solves, so the two can
never disagree about who a caller is and one tenant's cached solution can never
answer another's call. The cache is a bounded in-process LRU, and therefore **per worker**: a retry
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
error mapping, and idempotency; `test_request_ownership.py` drives ownership over
HTTP against the real router for every id-bearing tool — enumerated from the tool
catalog, cross-checked against the client methods that interpolate an id into a
solver URL path, with an import-time floor so a sweep that finds nothing fails
collection instead of reporting green.
