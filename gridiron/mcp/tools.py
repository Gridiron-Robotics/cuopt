# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The cuOpt tool catalog + dispatch (estate Contract A).

Six tools, split by what they cost and what they change:

  * ``assign_fleet_tasks`` — the domain tool. Robots + a worklist in, ordered
    per-robot task lists out. The cuOpt index model is encoded here rather than
    left to the caller (see :mod:`fleet_assignment`).
  * ``submit_cuopt_problem`` — the escape hatch for a caller that already has a
    native VRP/LP/MILP body. Kept explicitly separate so the domain tool stays
    the obvious choice.
  * ``get_solve_status`` / ``get_solve_result`` — poll and fetch.
  * ``cancel_solve`` — the only destructive tool: it drops a queued request.
  * ``solver_health`` — is the GPU service up.

``destructiveHint`` follows the estate rule: derived from what the handler
actually does to server state and defaulting to destructive when in doubt.
Solving is expensive but non-destructive — it creates a result, it does not
mutate anything a human would need to approve. Cancelling deletes a request, so
it is destructive and goes through the middleware HITL gate.
"""

from __future__ import annotations

import logging
from typing import Any

from gridiron.mcp.client import CuoptClient, CuoptError
from gridiron.mcp.fleet_assignment import (
    AssignmentError,
    decode_assignment,
    encode_assignment,
)

_log = logging.getLogger("gridiron.mcp.cuopt")

SERVER_NAME = "cuopt"


class ToolError(Exception):
    """A tool failed in a way the caller should see, with an HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_ROBOT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable robot/vehicle id; the solution is keyed by it."},
        "location": {"type": "integer", "description": "Index of the robot's current location in the cost matrix (0-based)."},
        "capacity": {"type": "integer", "description": "Units this robot can carry. Only honored when tasks also carry 'demand'."},
    },
    "required": ["id", "location"],
}

_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable task id (e.g. a pick line or move order)."},
        "location": {"type": "integer", "description": "Index of the task's location in the cost matrix (0-based)."},
        "demand": {"type": "integer", "description": "Units this task consumes of a robot's capacity."},
        "service_seconds": {"type": "number", "description": "Dwell time at the location, in seconds."},
    },
    "required": ["id", "location"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "assign_fleet_tasks",
        "description": (
            "Decide which robot performs which task, and in what order, minimizing "
            "total fleet travel. Takes the robots (each with a current location "
            "index) and the task worklist (each with a location index), plus either "
            "an explicit square cost_matrix over those location indices or "
            "coordinates from which a Manhattan-distance matrix is built (aisle "
            "travel, not straight-line). Returns each robot's ordered task list, "
            "arrival times, the total cost, and any task that could not be "
            "assigned. Solving is compute-only: it changes no ERP state and "
            "dispatches nothing. Runs synchronously up to time_limit_seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "robots": {"type": "array", "items": _ROBOT_SCHEMA, "minItems": 1},
                "tasks": {"type": "array", "items": _TASK_SCHEMA, "minItems": 1},
                "cost_matrix": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "Square travel-cost matrix indexed by location. Supply this or coordinates.",
                },
                "coordinates": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "[[x, y], …] per location index; a Manhattan matrix is derived from it.",
                },
                "time_limit_seconds": {
                    "type": "number",
                    "description": "Solver budget in seconds (default 10). Higher gives better routes, not different constraints.",
                },
                "return_to_start": {
                    "type": "boolean",
                    "description": "Whether each robot must end where it began (default true).",
                },
            },
            "required": ["robots", "tasks"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "submit_cuopt_problem",
        "description": (
            "Submit a native cuOpt VRP, LP or MILP request body to the solver and "
            "return its request id. For callers that already speak cuOpt's index "
            "model; prefer assign_fleet_tasks for fleet work, which builds that "
            "body correctly. Asynchronous: poll get_solve_status, then "
            "get_solve_result. Set validation_only to check a body without solving."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "problem": {"type": "object", "description": "A cuOpt request body (cost_matrix_data / fleet_data / task_data / solver_config, or an LP/MILP model)."},
                "validation_only": {"type": "boolean", "description": "Validate the body and return without solving."},
                "solver_logs": {"type": "boolean", "description": "Produce detailed solver logs retrievable from the solver's log endpoint."},
            },
            "required": ["problem"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_solve_status",
        "description": (
            "Check whether a submitted cuOpt request is still running, has "
            "completed, or has failed. Takes the request_id returned by "
            "submit_cuopt_problem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"request_id": {"type": "string", "description": "The reqId from submit_cuopt_problem."}},
            "required": ["request_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_solve_result",
        "description": (
            "Fetch the solution for a completed cuOpt request. For a routing "
            "problem the raw solver response is also decoded into per-robot "
            "ordered task lists. Returns an error if the request is still running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The reqId from submit_cuopt_problem."},
                "decode_routes": {"type": "boolean", "description": "Also return per-robot ordered task lists (default true)."},
            },
            "required": ["request_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "cancel_solve",
        "description": (
            "Delete a queued or cached cuOpt request, freeing solver capacity. "
            "DESTRUCTIVE: the request and any cached input data are dropped and "
            "cannot be recovered — a caller still polling it will get a 404. "
            "Requires human approval via the middleware HITL gate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"request_id": {"type": "string", "description": "The reqId to delete."}},
            "required": ["request_id"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "solver_health",
        "description": (
            "Report whether the cuOpt GPU solver service is reachable and ready. "
            "Use before submitting a large problem, and to distinguish 'solver "
            "down' from 'problem infeasible'."
        ),
        "input_schema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def validate_arguments(tool: str, arguments: dict[str, Any]) -> None:
    """Check declared ``required`` before dispatch. Raises 422 with per-field
    errors, so a malformed agent call never reaches the GPU."""
    spec = TOOLS_BY_NAME.get(tool)
    if spec is None:
        raise ToolError(404, f"unknown tool {tool!r}")
    schema = spec["input_schema"]
    errors = {
        field: "required"
        for field in schema.get("required", [])
        if arguments.get(field) in (None, "")
    }
    if errors:
        raise ToolError(422, f"invalid arguments for {tool!r}: {errors}")


def dispatch(tool: str, arguments: dict[str, Any], *, client: CuoptClient) -> Any:
    """Run a tool. Every failure is raised as :class:`ToolError` with a status —
    the transport layer never emits an unhandled 500."""
    validate_arguments(tool, arguments)
    try:
        return _dispatch(tool, arguments, client)
    except ToolError:
        raise
    except AssignmentError as exc:
        # The caller's model is wrong (bad index, missing capacity side). Their
        # fix, not ours, so it is a 400 and stays at warning level.
        _log.warning("cuopt tool %s rejected: %s", tool, exc)
        raise ToolError(400, str(exc)) from exc
    except CuoptError as exc:
        # The solver refused or is unreachable — an operational fault. ERROR so
        # the OpenObserve level=error alert fires the self-heal loop; a structured
        # 502 never produces one on its own.
        _log.error("cuopt tool %s failed against the solver: %s", tool, exc)
        raise ToolError(502, str(exc)) from exc
    except Exception as exc:
        _log.exception("cuopt tool %s raised unexpectedly", tool)
        raise ToolError(500, f"internal error in {tool!r}: {exc}") from exc


def _dispatch(tool: str, args: dict[str, Any], client: CuoptClient) -> Any:
    if tool == "assign_fleet_tasks":
        problem = encode_assignment(
            args["robots"],
            args["tasks"],
            cost_matrix=args.get("cost_matrix"),
            coordinates=args.get("coordinates"),
            time_limit_seconds=float(args.get("time_limit_seconds") or 10.0),
            return_to_start=bool(args.get("return_to_start", True)),
        )
        submitted = client.submit(problem)
        # A self-hosted solve returns the solution inline when it completes within
        # the request; otherwise only a reqId, which the caller polls.
        if isinstance(submitted, dict) and "response" in submitted:
            decoded = decode_assignment(submitted)
            return {"request_id": submitted.get("reqId"), **decoded}
        return {
            "request_id": _req_id(submitted),
            "status": "running",
            "feasible": None,
            "assignments": [],
            "note": (
                "The solver did not finish inside the submit call. Poll "
                "get_solve_status, then get_solve_result with decode_routes."
            ),
        }

    if tool == "submit_cuopt_problem":
        problem = args["problem"]
        if not isinstance(problem, dict):
            raise ToolError(400, "problem must be a JSON object")
        out = client.submit(
            problem,
            validation_only=bool(args.get("validation_only")),
            solver_logs=bool(args.get("solver_logs")),
        )
        return {"request_id": _req_id(out), "raw": out}

    if tool == "get_solve_status":
        return client.status(str(args["request_id"]))

    if tool == "get_solve_result":
        raw = client.solution(str(args["request_id"]))
        out: dict[str, Any] = {"raw": raw}
        if args.get("decode_routes", True) and isinstance(raw, dict):
            try:
                out["routing"] = decode_assignment(raw)
            except AssignmentError:
                # An LP/MILP solution has no vehicle_data. Not an error — the raw
                # result is still the answer.
                out["routing"] = None
        return out

    if tool == "cancel_solve":
        return {"request_id": args["request_id"], "deleted": client.cancel(str(args["request_id"]))}

    if tool == "solver_health":
        return {"server": SERVER_NAME, "healthy": True, "raw": client.health()}

    raise ToolError(404, f"unknown tool {tool!r}")  # pragma: no cover


def _req_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        rid = payload.get("reqId") or payload.get("req_id") or payload.get("id")
        return str(rid) if rid is not None else None
    return None


__all__ = ["TOOLS", "TOOLS_BY_NAME", "SERVER_NAME", "ToolError", "dispatch", "validate_arguments"]
