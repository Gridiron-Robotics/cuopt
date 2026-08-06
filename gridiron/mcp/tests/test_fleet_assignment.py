# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The cuOpt index model, encoded and decoded.

cuOpt speaks positions in a cost matrix; the estate speaks robot and task ids.
Every test here is a way that translation can be wrong while still producing a
payload the solver ACCEPTS — which is the dangerous class, because the answer
comes back looking authoritative and describes the wrong robots.

The request/response shapes asserted against are upstream's own documented
examples (``cuopt_server/utils/routing/data_definition.py``: ``vrp_example_data``
and ``vrp_response``), so a drift in cuOpt's contract shows up here.
"""

import pytest

from gridiron.mcp.fleet_assignment import (
    AssignmentError,
    build_cost_matrix,
    decode_assignment,
    encode_assignment,
)

ROBOTS = [
    {"id": "amr-1", "location": 0},
    {"id": "amr-2", "location": 3},
]
TASKS = [
    {"id": "pick-A", "location": 1},
    {"id": "pick-B", "location": 2},
]
MATRIX = [
    [0, 4, 6, 2],
    [4, 0, 3, 5],
    [6, 3, 0, 7],
    [2, 5, 7, 0],
]


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def test_encode_produces_the_upstream_request_shape():
    body = encode_assignment(ROBOTS, TASKS, cost_matrix=MATRIX)
    assert set(body) == {
        "cost_matrix_data",
        "fleet_data",
        "task_data",
        "solver_config",
    }
    # cuOpt keys the matrix by vehicle TYPE. A single unnamed type must be one
    # key, or upstream validation rejects with "Set vehicle types when using
    # multiple matrices".
    assert list(body["cost_matrix_data"]["data"]) == ["0"]
    assert "vehicle_types" not in body["fleet_data"]


def test_ids_and_locations_stay_aligned():
    """The mistake that yields a plausible wrong answer: ids in one order,
    locations in another."""
    body = encode_assignment(ROBOTS, TASKS, cost_matrix=MATRIX)
    assert body["fleet_data"]["vehicle_ids"] == ["amr-1", "amr-2"]
    assert body["fleet_data"]["vehicle_locations"] == [[0, 0], [3, 3]]
    assert body["task_data"]["task_ids"] == ["pick-A", "pick-B"]
    assert body["task_data"]["task_locations"] == [1, 2]


def test_out_of_range_location_is_refused_before_the_solver():
    with pytest.raises(AssignmentError, match="outside the 4-location cost matrix"):
        encode_assignment(
            ROBOTS, [{"id": "pick-A", "location": 9}], cost_matrix=MATRIX
        )


def test_non_integer_location_is_refused():
    with pytest.raises(AssignmentError, match="integer matrix index"):
        encode_assignment(
            ROBOTS, [{"id": "pick-A", "location": "aisle-3"}], cost_matrix=MATRIX
        )


def test_duplicate_ids_are_refused():
    """The solution is keyed by id: two robots sharing one id means one of them
    silently absorbs the other's route."""
    with pytest.raises(AssignmentError, match="robot ids must be unique"):
        encode_assignment(
            [{"id": "amr-1", "location": 0}, {"id": "amr-1", "location": 1}],
            TASKS,
            cost_matrix=MATRIX,
        )
    with pytest.raises(AssignmentError, match="task ids must be unique"):
        encode_assignment(
            ROBOTS,
            [{"id": "pick-A", "location": 1}, {"id": "pick-A", "location": 2}],
            cost_matrix=MATRIX,
        )


def test_empty_fleet_or_worklist_is_refused():
    with pytest.raises(AssignmentError, match="no robots"):
        encode_assignment([], TASKS, cost_matrix=MATRIX)
    with pytest.raises(AssignmentError, match="no tasks"):
        encode_assignment(ROBOTS, [], cost_matrix=MATRIX)


def test_non_square_matrix_is_refused():
    with pytest.raises(AssignmentError, match="square"):
        encode_assignment(ROBOTS, TASKS, cost_matrix=[[0, 1], [1, 0], [1, 1]])


def test_capacity_needs_both_sides():
    """A demand with no capacity makes every task unservable; a capacity with no
    demand constrains nothing. Either alone is a modelling error the solver would
    not report."""
    with pytest.raises(AssignmentError, match="both task 'demand' and robot 'capacity'"):
        encode_assignment(
            ROBOTS, [{"id": "p", "location": 1, "demand": 3}], cost_matrix=MATRIX
        )
    with pytest.raises(AssignmentError, match="both task 'demand' and robot 'capacity'"):
        encode_assignment(
            [{"id": "amr-1", "location": 0, "capacity": 5}],
            TASKS,
            cost_matrix=MATRIX,
        )


def test_capacity_is_emitted_as_the_single_dimension_cuopt_expects():
    body = encode_assignment(
        [{"id": "amr-1", "location": 0, "capacity": 5}],
        [{"id": "p", "location": 1, "demand": 3}],
        cost_matrix=MATRIX,
    )
    # cuOpt models capacity as a list of dimensions; one dimension is one inner
    # list, not a bare list of scalars.
    assert body["task_data"]["demand"] == [[3]]
    assert body["fleet_data"]["capacities"] == [[5]]


def test_return_to_start_false_drops_the_return_trip():
    body = encode_assignment(ROBOTS, TASKS, cost_matrix=MATRIX, return_to_start=False)
    assert body["fleet_data"]["drop_return_trips"] == [True, True]


def test_coordinates_build_a_manhattan_matrix():
    """Aisle travel, not straight line: Euclidean would systematically under-cost
    every route in a warehouse."""
    m = build_cost_matrix([[0, 0], [3, 4]])
    assert m[0][1] == 7  # |3| + |4|, not 5
    assert m[0][0] == 0
    assert m[1][0] == m[0][1]


def test_coordinates_reject_a_malformed_point():
    with pytest.raises(AssignmentError, match=r"coordinates\[1\] must be \[x, y\]"):
        build_cost_matrix([[0, 0], [1, 2, 3]])


def test_encode_requires_a_location_source():
    with pytest.raises(AssignmentError, match="cost_matrix or coordinates"):
        encode_assignment(ROBOTS, TASKS)


# --------------------------------------------------------------------------- #
# Decoding — upstream's own documented response
# --------------------------------------------------------------------------- #
_UPSTREAM_RESPONSE = {
    "response": {
        "solver_response": {
            "status": 0,
            "num_vehicles": 2,
            "solution_cost": 2.0,
            "objective_values": {"cost": 2.0},
            "vehicle_data": {
                "veh-1": {
                    "task_id": ["Break", "Task-A"],
                    "arrival_stamp": [1.0, 2.0],
                    "type": ["Break", "Delivery"],
                    "route": [1, 1],
                },
                "veh-2": {
                    "task_id": ["Depot", "Break", "Task-B", "Depot"],
                    "arrival_stamp": [2.0, 2.0, 4.0, 5.0],
                    "type": ["Depot", "Break", "Delivery", "Depot"],
                    "route": [0, 0, 2, 0],
                },
            },
            "dropped_tasks": {"task_id": [], "task_index": []},
        }
    },
    "reqId": "e8421e9e-e42e-4511-8da2-314253667dcf",
}


def test_decode_strips_routing_artifacts_from_the_task_order():
    """Depot and Break stops are routing artifacts. Reporting them as work would
    tell a dispatcher to send a robot to pick "Break"."""
    out = decode_assignment(_UPSTREAM_RESPONSE)
    by_robot = {a["robot_id"]: a for a in out["assignments"]}
    assert by_robot["veh-1"]["task_order"] == ["Task-A"]
    assert by_robot["veh-2"]["task_order"] == ["Task-B"]
    assert by_robot["veh-1"]["task_count"] == 1


def test_decode_keeps_arrival_stamps_aligned_after_stripping():
    """The stamp index must follow the ORIGINAL stop list — re-indexing after the
    strip shifts every arrival time by the number of artifacts removed."""
    out = decode_assignment(_UPSTREAM_RESPONSE)
    by_robot = {a["robot_id"]: a for a in out["assignments"]}
    assert by_robot["veh-1"]["stops"][0]["arrival"] == 2.0  # not 1.0 (the Break)
    assert by_robot["veh-2"]["stops"][0]["arrival"] == 4.0  # not 2.0


def test_decode_reports_feasibility_and_cost():
    out = decode_assignment(_UPSTREAM_RESPONSE)
    assert out["feasible"] is True
    assert out["status"] == 0
    assert out["total_cost"] == 2.0
    assert out["robots_used"] == 2
    assert out["unassigned_task_ids"] == []


def test_infeasible_status_is_not_reported_as_feasible():
    """cuOpt returns status 1 with a solution attached for an infeasible problem.
    Treating "a solution came back" as success dispatches robots on a route that
    violates the constraints."""
    payload = {
        "response": {
            "solver_response": {
                "status": 1,
                "solution_cost": 99.0,
                "vehicle_data": {},
                "dropped_tasks": {"task_id": ["pick-B"], "task_index": [1]},
            }
        }
    }
    out = decode_assignment(payload)
    assert out["feasible"] is False
    assert out["unassigned_task_ids"] == ["pick-B"]


def test_decode_accepts_an_already_unwrapped_solver_response():
    out = decode_assignment(_UPSTREAM_RESPONSE["response"])
    assert out["feasible"] is True
    assert len(out["assignments"]) == 2


def test_assignments_are_ordered_deterministically():
    """Two runs must not report the same solution in different orders — a diff of
    two dispatch plans should show real changes only."""
    a = decode_assignment(_UPSTREAM_RESPONSE)
    b = decode_assignment(_UPSTREAM_RESPONSE)
    assert [x["robot_id"] for x in a["assignments"]] == [
        x["robot_id"] for x in b["assignments"]
    ] == ["veh-1", "veh-2"]
