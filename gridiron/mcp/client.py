# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin HTTP client for the upstream cuOpt server API.

Upstream NVIDIA cuOpt is unmodified (house rule); this is the Gridiron seam that
speaks its self-hosted REST contract:

    POST   /cuopt/request        submit VRP / LP / MILP  -> {"reqId": ...}
    GET    /cuopt/request/{id}   poll status
    GET    /cuopt/solution/{id}  fetch the solution
    DELETE /cuopt/request/{id}   drop a queued/cached request
    GET    /cuopt/health         liveness

The solver is **asynchronous by design**: a submit returns a request id and the
caller polls. That shape is preserved rather than hidden behind a blocking call,
because a VRP solve can run for minutes and an agent that blocks on it holds a
tool slot the whole time.

The transport is injected so every code path here is testable without a GPU or a
running solver.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "http://localhost:5000"
DEFAULT_TIMEOUT = 30.0


class CuoptError(RuntimeError):
    """The solver was reachable but refused, or was not reachable at all."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Response:
    status: int
    body: Any


# (method, url, json_body, headers, timeout) -> Response
Transport = Callable[[str, str, Any, dict[str, str], float], Response]


def _httpx_transport(
    method: str, url: str, body: Any, headers: dict[str, str], timeout: float
) -> Response:
    import httpx

    resp = httpx.request(
        method, url, json=body, headers=headers, timeout=timeout
    )
    try:
        parsed: Any = resp.json()
    except (ValueError, json.JSONDecodeError):
        parsed = resp.text
    return Response(status=resp.status_code, body=parsed)


class CuoptClient:
    """Calls the cuOpt server. Never raises a bare transport exception at the
    caller — a network failure becomes :class:`CuoptError`, which the MCP layer
    turns into a structured tool error."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("CUOPT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("CUOPT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)
        )
        self._transport = transport or _httpx_transport

    def _call(
        self, method: str, path: str, *, body: Any = None, query: str = ""
    ) -> Any:
        url = f"{self.base_url}{path}{query}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            resp = self._transport(method, url, body, headers, self.timeout)
        except Exception as exc:  # transport-level: DNS, refused, timeout
            raise CuoptError(f"cuOpt server unreachable at {url}: {exc}") from exc
        if resp.status >= 400:
            raise CuoptError(
                f"cuOpt server returned {resp.status} for {method} {path}: "
                f"{_short(resp.body)}",
                status=resp.status,
            )
        return resp.body

    # ---- the solver surface ------------------------------------------------ #
    def submit(
        self,
        problem: dict[str, Any],
        *,
        validation_only: bool = False,
        solver_logs: bool = False,
    ) -> Any:
        params = []
        if validation_only:
            params.append("validation_only=true")
        if solver_logs:
            params.append("solver_logs=true")
        query = f"?{'&'.join(params)}" if params else ""
        return self._call("POST", "/cuopt/request", body=problem, query=query)

    def status(self, request_id: str) -> Any:
        return self._call("GET", f"/cuopt/request/{request_id}")

    def solution(self, request_id: str) -> Any:
        return self._call("GET", f"/cuopt/solution/{request_id}")

    def cancel(self, request_id: str) -> Any:
        return self._call("DELETE", f"/cuopt/request/{request_id}")

    def health(self) -> Any:
        return self._call("GET", "/cuopt/health")


def _short(body: Any, limit: int = 300) -> str:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return text[:limit]


__all__ = ["CuoptClient", "CuoptError", "Response", "Transport", "DEFAULT_BASE_URL"]
