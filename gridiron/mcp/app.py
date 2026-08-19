# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract-A MCP transport for cuOpt (``GET /tools``, ``POST /invoke``, ``HEAD /``).

Two ways to run it, same router:

  * **Sidecar** (default, no GPU needed): ``uvicorn gridiron.mcp.app:app``. It
    proxies to a cuOpt server at ``CUOPT_BASE_URL``, so the tool surface can live
    anywhere and the GPU box stays a pure solver.
  * **Co-located**: :func:`build_mcp_router` mounted onto the upstream cuOpt app
    by ``gridiron.observability.asgi``, serving both on one port.

Auth is **fail-closed**: with neither ``CUOPT_MCP_TOKEN`` set nor
``CUOPT_MCP_ALLOW_INSECURE=true``, every tool route answers 503 and refuses to
serve. An unauthenticated tool surface on a solver is not merely a data-exposure
problem — it is free GPU time for anyone who can reach the port. ``HEAD /`` stays
open so a gateway can probe liveness either way.

**What ``X-Tenant-Id`` does, and what it is worth.** It is half of the caller's
*principal* — ``(sha256(bearer)[:16], X-Tenant-Id)`` — which scopes the
idempotency replay cache and owns the solves it creates. Upstream cuOpt attaches
no owner to a ``reqId``, so the owner map lives here
(:mod:`gridiron.mcp.ownership`): the principal is recorded when a solve is
created and checked before ``get_solve_status`` / ``get_solve_result`` /
``cancel_solve`` touch the solver.

Enforcement is gated on ``CUOPT_MCP_ENFORCE_REQUEST_OWNER``, default **off**, and
the reason is stated rather than glossed: the tenant half of the principal is
caller-asserted, so with one shared bearer token the check contains accidents (an
agent or gateway carrying a ``reqId`` across tenant contexts, a destructive
cross-tenant ``cancel_solve``) but not a hostile holder of that token, who can
simply assert the victim's tenant. It becomes a real authorization boundary only
once the estate issues a credential per tenant — at which point the
authenticated half becomes distinct and no code here changes. See
:mod:`gridiron.mcp.ownership` for the two preconditions (single worker or shared
store; registry capacity) before turning the flag on.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from gridiron.mcp.client import CuoptClient
from gridiron.mcp import ownership
from gridiron.mcp.tools import SERVER_NAME, TOOLS, ToolError, dispatch

_log = logging.getLogger("gridiron.mcp.cuopt")

MAX_REPLAY_ENTRIES = 2048


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def auth_mode(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else dict(os.environ)
    if (env.get("CUOPT_MCP_TOKEN") or "").strip():
        return "token"
    if _truthy(env.get("CUOPT_MCP_ALLOW_INSECURE")):
        return "insecure-explicitly-allowed"
    return "fail-closed"


def _check_bearer(authorization: str | None, env: dict[str, str]) -> tuple[bool, int, str]:
    expected = (env.get("CUOPT_MCP_TOKEN") or "").strip()
    if not expected:
        if _truthy(env.get("CUOPT_MCP_ALLOW_INSECURE")):
            return True, 200, ""
        return (
            False,
            503,
            "cuOpt MCP is not configured for authenticated access: CUOPT_MCP_TOKEN "
            "is unset. Refusing to serve solver tools, which would be unmetered GPU "
            "time for any caller. Set CUOPT_MCP_TOKEN, or set "
            "CUOPT_MCP_ALLOW_INSECURE=true for local development only.",
        )
    presented = (authorization or "").strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    if not presented:
        return False, 401, "missing bearer token"
    if not hmac.compare_digest(presented, expected):
        return False, 403, "bearer token not accepted"
    return True, 200, ""


class _ReplayCache:
    """Principal-scoped (tool, Idempotency-Key) -> result, bounded LRU.

    The first element of the key is the caller principal from
    :func:`gridiron.mcp.ownership.principal` — the bearer's fingerprint AND the
    asserted tenant, not the tenant alone.

    In-process, and therefore per-worker: a retry that lands on another worker
    re-solves. That is a cost, not a correctness problem, because every cuOpt tool
    is idempotent by construction (solving twice yields another solution; deleting
    twice is still deleted). Documented rather than hidden — a module whose tools
    mutated business state would need Redis here.
    """

    def __init__(self, cap: int = MAX_REPLAY_ENTRIES) -> None:
        self._d: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._cap = cap

    def get(self, key: tuple[str, str, str]) -> Any:
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return None

    def put(self, key: tuple[str, str, str], value: Any) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)


def build_mcp_router(
    *,
    client: CuoptClient | None = None,
    env: dict[str, str] | None = None,
    replay: _ReplayCache | None = None,
    owners: ownership.OwnerRegistry | None = None,
) -> APIRouter:
    """The Contract-A router. ``client``/``env`` injected for tests."""
    router = APIRouter(tags=["mcp"])
    cache = replay if replay is not None else _ReplayCache()
    owner_registry = owners if owners is not None else ownership.OwnerRegistry()

    def _env() -> dict[str, str]:
        return env if env is not None else dict(os.environ)

    def _client() -> CuoptClient:
        return client if client is not None else CuoptClient()

    @router.head("/")
    async def mcp_head() -> Response:
        # Unauthenticated on purpose: a gateway must be able to probe liveness
        # without holding a credential, and this leaks nothing.
        return Response(status_code=200)

    @router.get("/tools")
    async def list_tools(
        server: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        ok, status, message = _check_bearer(authorization, _env())
        if not ok:
            return _error(status, message)
        # An unknown server filter is an empty catalog, not an error — a gateway
        # enumerating every module must not fail on the ones that do not match.
        if server is not None and server != SERVER_NAME:
            return JSONResponse({"tools": []})
        return JSONResponse({"server": SERVER_NAME, "tools": TOOLS})

    @router.post("/invoke")
    async def invoke(
        request: Request,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        ok, status, message = _check_bearer(authorization, _env())
        if not ok:
            return _error(status, message)
        try:
            payload = await request.json()
        except Exception:
            return _error(400, "body must be JSON")
        if not isinstance(payload, dict):
            return _error(400, "body must be a JSON object")

        server = payload.get("server")
        if server is not None and server != SERVER_NAME:
            return _error(404, f"unknown server {server!r}")
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            return _error(400, "'tool' is required")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(400, "'arguments' must be an object")

        # The caller's identity: the authenticated bearer's fingerprint plus the
        # asserted tenant. It scopes the replay cache AND owns the solves this
        # call creates, so the two can never disagree about who a caller is —
        # keying replay on the tenant alone let two different credentials
        # asserting one tenant share cached results, including a request_id
        # only one of them would then be allowed to poll.
        owner = ownership.principal(authorization, x_tenant_id)
        enforce_owner = ownership.enforcing(_env())

        # BEFORE the replay shortcut and BEFORE dispatch, in that order and for
        # two different reasons. Before dispatch because cancel_solve is
        # destructive: a check that runs after the handler has already deleted
        # another caller's request refuses nothing. Before the replay lookup
        # because an early return on a cached result is an authorization
        # shortcut — a request the caller is not entitled to make must not be
        # answered 200 just because an entry happens to sit under its
        # (principal, tool, Idempotency-Key).
        denied = ownership.refusal(
            tool, arguments, owner, owner_registry, enabled=enforce_owner
        )
        if denied is not None:
            _log.warning("cuopt %s refused: request not owned by caller", tool)
            return _error(403, denied, tool=tool)

        if idempotency_key:
            hit = cache.get((owner, tool, idempotency_key))
            if hit is not None:
                # Re-record: the replay cache and the owner registry evict
                # independently, and a replayed request_id whose ownership had
                # aged out would be unpollable by the caller that just got it.
                ownership.record_result(tool, hit, owner, owner_registry)
                return JSONResponse({"tool": tool, "result": hit, "replayed": True})

        try:
            result = dispatch(tool, arguments, client=_client())
        except ToolError as exc:
            return _error(exc.status, exc.message, tool=tool)

        # Recorded unconditionally, not only when enforcing, so switching the
        # flag on does not start from an empty map and 403 live traffic.
        ownership.record_result(tool, result, owner, owner_registry)

        if idempotency_key:
            cache.put((owner, tool, idempotency_key), result)
        return JSONResponse({"tool": tool, "result": result})

    return router


def _error(status: int, message: str, *, tool: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    if tool:
        body["tool"] = tool
    headers = {"Retry-After": "30"} if status == 503 else None
    return JSONResponse(body, status_code=status, headers=headers)


def build_app(**kwargs: Any) -> FastAPI:
    """Standalone sidecar app. Wires the self-heal drop-in so a solver fault that
    surfaces here reaches OpenObserve on the same ``cuopt`` stream."""
    # openapi_url=None alongside docs_url/redoc_url. Disabling only the two docs
    # UIs left /openapi.json serving the same information to anyone: an
    # unauthenticated GET returned the full schema — every path (/tools,
    # /invoke) and every request shape — from a service whose entire posture is
    # "refuse to serve tools without a credential". Handing an attacker the tool
    # catalog's shape for free contradicts that, so the generated schema route
    # goes too. Contract-A discovery is `GET /tools`, which is authenticated;
    # nothing in the estate consumes this app's OpenAPI document.
    app = FastAPI(
        title="cuOpt MCP (Gridiron)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(build_mcp_router(**kwargs))

    try:
        from gridiron.observability.gridiron_otel import setup_observability

        setup_observability(SERVER_NAME, app=app)
    except Exception:  # pragma: no cover - observability must never block boot
        _log.warning("observability drop-in unavailable; MCP serving without OTLP")
    return app


app = build_app()

__all__ = ["app", "build_app", "build_mcp_router", "auth_mode", "MAX_REPLAY_ENTRIES"]
