# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fleet task assignment: robots + a worklist -> a cuOpt VRP, and back.

This is the piece that makes the solver usable by an agent. cuOpt's request body
is an index-space model — vehicles and tasks are positions in a cost matrix, and
the answer comes back keyed by those positions. An LLM asked to author that
directly will get the indexing wrong in a way that still validates, which is the
worst failure mode available: a feasible-looking route for the wrong robots.

So the encoding is code, not prompt:

  * :func:`encode_assignment` takes domain objects — robots with a current
    location, tasks with a pick location — and produces the cuOpt payload,
    assigning matrix indices itself.
  * :func:`decode_assignment` maps the solver's response back to robot and task
    IDs, in visit order, and reports what it could not assign.

Both are pure functions over plain dicts: no GPU, no solver, no network, so the
index arithmetic is exercised in unit tests rather than discovered in production.

Location model: callers give either an explicit ``cost_matrix`` (row/col per
location index) plus integer ``location`` per robot/task, or ``coordinates`` per
named location, from which a Manhattan-distance matrix is built. Manhattan rather
than Euclidean because warehouse travel follows aisles, and a straight-line
estimate systematically under-costs every route.
"""

from __future__ import annotations

from typing import Any

DEPOT_TYPES = frozenset({"Depot", "Break"})


class AssignmentError(ValueError):
    """The request cannot be encoded — refused before the solver is called."""


def _manhattan(a: tuple[float, float], b: tuple[float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def build_cost_matrix(coordinates: list[list[float]]) -> list[list[float]]:
    """Manhattan-distance matrix over ``[[x, y], …]`` location coordinates."""
    if not coordinates:
        raise AssignmentError("coordinates must not be empty")
    pts: list[tuple[float, float]] = []
    for i, c in enumerate(coordinates):
        if not isinstance(c, (list, tuple)) or len(c) != 2:
            raise AssignmentError(
                f"coordinates[{i}] must be [x, y], got {c!r}"
            )
        pts.append((float(c[0]), float(c[1])))
    return [[_manhattan(p, q) for q in pts] for p in pts]


def encode_assignment(
    robots: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    cost_matrix: list[list[float]] | None = None,
    coordinates: list[list[float]] | None = None,
    time_limit_seconds: float = 10.0,
    return_to_start: bool = True,
) -> dict[str, Any]:
    """Encode a fleet worklist as a cuOpt VRP request body.

    ``robots``: ``{"id": str, "location": int, "capacity": int?}``
    ``tasks``:  ``{"id": str, "location": int, "demand": int?, "service_seconds": num?}``

    Every location index is bounds-checked against the matrix here. cuOpt would
    otherwise accept an out-of-range index on some paths and return a route that
    silently references a location that does not exist.
    """
    if not robots:
        raise AssignmentError("no robots supplied — nothing can be assigned")
    if not tasks:
        raise AssignmentError("no tasks supplied — nothing to assign")

    matrix = cost_matrix if cost_matrix is not None else (
        build_cost_matrix(coordinates) if coordinates is not None else None
    )
    if matrix is None:
        raise AssignmentError("supply either cost_matrix or coordinates")
    n = len(matrix)
    if any(len(row) != n for row in matrix):
        raise AssignmentError(f"cost_matrix must be square; got {n} rows of uneven width")

    def _loc(entity: dict[str, Any], kind: str, i: int) -> int:
        if "location" not in entity:
            raise AssignmentError(f"{kind}[{i}] ({entity.get('id')!r}) has no 'location'")
        loc = entity["location"]
        if not isinstance(loc, int) or isinstance(loc, bool):
            raise AssignmentError(
                f"{kind}[{i}] location must be an integer matrix index, got {loc!r}"
            )
        if not 0 <= loc < n:
            raise AssignmentError(
                f"{kind}[{i}] ({entity.get('id')!r}) location {loc} is outside the "
                f"{n}-location cost matrix"
            )
        return loc

    robot_ids = [str(r.get("id") or f"robot-{i}") for i, r in enumerate(robots)]
    task_ids = [str(t.get("id") or f"task-{i}") for i, t in enumerate(tasks)]
    if len(set(robot_ids)) != len(robot_ids):
        raise AssignmentError("robot ids must be unique — the solution is keyed by them")
    if len(set(task_ids)) != len(task_ids):
        raise AssignmentError("task ids must be unique — the solution is keyed by them")

    robot_locs = [_loc(r, "robots", i) for i, r in enumerate(robots)]
    task_locs = [_loc(t, "tasks", i) for i, t in enumerate(tasks)]

    fleet: dict[str, Any] = {
        # [start, end] per vehicle. Ending where it started keeps a robot from
        # parking at the last drop, which is what a real fleet needs unless the
        # caller says otherwise.
        "vehicle_locations": [
            [loc, loc if return_to_start else loc] for loc in robot_locs
        ],
        "vehicle_ids": robot_ids,
    }
    if not return_to_start:
        fleet["drop_return_trips"] = [True] * len(robots)

    task_data: dict[str, Any] = {"task_locations": task_locs, "task_ids": task_ids}

    # Capacity is only sent when the caller models it on BOTH sides. A demand
    # without capacities makes every task unservable; a capacity without demands
    # is a constraint on nothing.
    demands = [t.get("demand") for t in tasks]
    caps = [r.get("capacity") for r in robots]
    if any(d is not None for d in demands) and any(c is not None for c in caps):
        task_data["demand"] = [[int(d or 0) for d in demands]]
        fleet["capacities"] = [[int(c or 0) for c in caps]]
    elif any(d is not None for d in demands) != any(c is not None for c in caps):
        raise AssignmentError(
            "capacity modelling needs both task 'demand' and robot 'capacity'; "
            "got only one side, which would constrain nothing or everything"
        )

    if any(t.get("service_seconds") is not None for t in tasks):
        task_data["service_times"] = [
            float(t.get("service_seconds") or 0) for t in tasks
        ]

    return {
        "cost_matrix_data": {"data": {"0": matrix}},
        "fleet_data": fleet,
        "task_data": task_data,
        "solver_config": {"time_limit": float(time_limit_seconds)},
    }


def decode_assignment(solution: dict[str, Any]) -> dict[str, Any]:
    """Map a cuOpt VRP response back to per-robot ordered task lists.

    Depot and Break stops are dropped from the task order — they are routing
    artifacts, not work — but the arrival stamps are kept aligned so a caller can
    still read when each real task is reached.
    """
    resp = solution.get("response", solution) if isinstance(solution, dict) else {}
    solver = resp.get("solver_response") or resp.get("solver_infeasible_response") or {}
    if not isinstance(solver, dict):
        raise AssignmentError(f"unrecognized cuOpt solution shape: {type(solver)}")

    status = solver.get("status")
    assignments: list[dict[str, Any]] = []
    for robot_id, data in (solver.get("vehicle_data") or {}).items():
        ids = list(data.get("task_id") or [])
        types = list(data.get("type") or [])
        stamps = list(data.get("arrival_stamp") or [])
        ordered: list[dict[str, Any]] = []
        for i, tid in enumerate(ids):
            kind = types[i] if i < len(types) else ""
            if kind in DEPOT_TYPES or tid in DEPOT_TYPES:
                continue
            ordered.append(
                {
                    "task_id": tid,
                    "arrival": stamps[i] if i < len(stamps) else None,
                    "type": kind or None,
                }
            )
        assignments.append(
            {
                "robot_id": robot_id,
                "task_count": len(ordered),
                "stops": ordered,
                "task_order": [s["task_id"] for s in ordered],
            }
        )

    dropped = solver.get("dropped_tasks") or {}
    return {
        # 0 = feasible, 1 = infeasible-but-available (cuOpt's own convention).
        "status": status,
        "feasible": status == 0,
        "total_cost": solver.get("solution_cost"),
        "robots_used": solver.get("num_vehicles"),
        "assignments": sorted(assignments, key=lambda a: a["robot_id"]),
        "unassigned_task_ids": list(dropped.get("task_id") or []),
        "objective_values": solver.get("objective_values") or {},
    }


__all__ = [
    "AssignmentError",
    "build_cost_matrix",
    "encode_assignment",
    "decode_assignment",
]
