# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract A on the cuOpt overlay: catalog shape, fail-closed auth, dispatch.

No GPU and no solver: the cuOpt HTTP transport is injected, so these tests cover
the seam that a live GPU box would otherwise be the first thing to exercise.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from gridiron.mcp.app import auth_mode, build_app
from gridiron.mcp.client import CuoptClient, CuoptError, Response
from gridiron.mcp.tools import TOOLS, TOOLS_BY_NAME, ToolError, dispatch

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ENV = {"CUOPT_MCP_TOKEN": TOKEN}

_SOLVED = {
    "reqId": "req-1",
    "response": {
        "solver_response": {
            "status": 0,
            "num_vehicles": 1,
            "solution_cost": 8.0,
            "vehicle_data": {
                "amr-1": {
                    "task_id": ["Depot", "pick-A", "Depot"],
                    "arrival_stamp": [0.0, 4.0, 8.0],
                    "type": ["Depot", "Delivery", "Depot"],
                }
            },
            "dropped_tasks": {"task_id": [], "task_index": []},
        }
    },
}

ROBOTS = [{"id": "amr-1", "location": 0}]
TASKS = [{"id": "pick-A", "location": 1}]
MATRIX = [[0, 4], [4, 0]]


def _fake_transport(responses):
    """Transport returning queued Responses, recording each call."""
    calls = []

    def transport(method, url, body, headers, timeout):
        calls.append({"method": method, "url": url, "body": body})
        return responses.pop(0) if responses else Response(200, {})

    transport.calls = calls
    return transport


def _client_for(*responses):
    return CuoptClient(
        "http://solver:5000", transport=_fake_transport(list(responses))
    )


def _app(client=None, env=None):
    return TestClient(build_app(client=client, env=env if env is not None else ENV))


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def test_every_tool_has_a_real_input_schema():
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert isinstance(schema.get("required", []), list)
        assert tool["description"].strip(), f"{tool['name']} has no description"
        # Descriptions are consumed by an LLM as instructions; a one-liner like
        # "solves a problem" is not usable.
        assert len(tool["description"]) > 80, f"{tool['name']} description too thin"


def test_only_the_deleting_tool_is_destructive():
    """destructiveHint fires the middleware HITL approval gate. Marking a solve
    destructive would put a human in front of every route computation; NOT marking
    the delete would let an agent drop a queued request unapproved."""
    destructive = {
        t["name"] for t in TOOLS if t["annotations"]["destructiveHint"] is True
    }
    assert destructive == {"cancel_solve"}


def test_every_advertised_tool_has_a_dispatch_branch():
    """A catalog entry with no handler advertises a capability that 404s."""
    client = _client_for(*[Response(200, {}) for _ in range(4)])
    for tool in TOOLS:
        try:
            dispatch(tool["name"], {}, client=client)
        except ToolError as exc:
            assert exc.status != 404 or "unknown tool" not in exc.message, (
                f"{tool['name']} is advertised but unhandled"
            )
        except Exception:
            pass


def test_tools_endpoint_filters_by_server_without_erroring():
    with _app(_client_for()) as c:
        assert c.get("/tools", headers=AUTH).json()["tools"]
        # A gateway enumerating every module must not fail on a non-match.
        r = c.get("/tools?server=not-cuopt", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["tools"] == []


# --------------------------------------------------------------------------- #
# Fail-closed auth
# --------------------------------------------------------------------------- #
def test_unset_token_refuses_to_serve_tools():
    """An open tool surface on a solver is unmetered GPU time for any caller."""
    assert auth_mode({}) == "fail-closed"
    with _app(_client_for(), env={}) as c:
        for resp in (
            c.get("/tools"),
            c.post("/invoke", json={"tool": "solver_health"}),
        ):
            assert resp.status_code == 503
            assert "CUOPT_MCP_TOKEN" in resp.json()["error"]
            assert resp.headers["Retry-After"] == "30"


def test_health_probe_stays_open_when_fail_closed():
    """A gateway must be able to see the process is alive without a credential."""
    with _app(_client_for(), env={}) as c:
        assert c.head("/").status_code == 200


def test_explicit_insecure_opt_in_is_the_only_way_to_reopen():
    assert auth_mode({"CUOPT_MCP_ALLOW_INSECURE": "true"}) == "insecure-explicitly-allowed"
    with _app(_client_for(Response(200, {"status": "ok"})),
              env={"CUOPT_MCP_ALLOW_INSECURE": "true"}) as c:
        assert c.post("/invoke", json={"tool": "solver_health"}).status_code == 200


def test_missing_and_wrong_bearer_are_distinguished():
    with _app(_client_for()) as c:
        assert c.get("/tools").status_code == 401
        assert c.get("/tools", headers={"Authorization": "Bearer nope"}).status_code == 403


# --------------------------------------------------------------------------- #
# Dispatch + errors
# --------------------------------------------------------------------------- #
def test_required_arguments_are_validated_before_the_solver_is_called():
    transport = _fake_transport([])
    client = CuoptClient("http://solver:5000", transport=transport)
    with _app(client) as c:
        r = c.post("/invoke", json={"tool": "get_solve_status", "arguments": {}}, headers=AUTH)
        assert r.status_code == 422
        assert "request_id" in r.json()["error"]
    assert transport.calls == [], "a malformed call must not reach the solver"


def test_assign_fleet_tasks_returns_decoded_routes():
    client = _client_for(Response(200, _SOLVED))
    with _app(client) as c:
        r = c.post(
            "/invoke",
            json={
                "server": "cuopt",
                "tool": "assign_fleet_tasks",
                "arguments": {"robots": ROBOTS, "tasks": TASKS, "cost_matrix": MATRIX},
            },
            headers=AUTH,
        )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["feasible"] is True
    assert result["assignments"][0]["robot_id"] == "amr-1"
    assert result["assignments"][0]["task_order"] == ["pick-A"]
    assert result["request_id"] == "req-1"


def test_a_pending_solve_says_so_instead_of_claiming_no_assignments():
    """Submit returned only a reqId. Reporting an empty assignment list as the
    ANSWER would read as "no robot should do anything"."""
    client = _client_for(Response(200, {"reqId": "req-2"}))
    with _app(client) as c:
        result = c.post(
            "/invoke",
            json={
                "tool": "assign_fleet_tasks",
                "arguments": {"robots": ROBOTS, "tasks": TASKS, "cost_matrix": MATRIX},
            },
            headers=AUTH,
        ).json()["result"]
    assert result["status"] == "running"
    assert result["feasible"] is None
    assert "get_solve_status" in result["note"]


def test_a_bad_model_is_400_and_never_reaches_the_solver():
    transport = _fake_transport([])
    client = CuoptClient("http://solver:5000", transport=transport)
    with _app(client) as c:
        r = c.post(
            "/invoke",
            json={
                "tool": "assign_fleet_tasks",
                "arguments": {
                    "robots": ROBOTS,
                    "tasks": [{"id": "p", "location": 99}],
                    "cost_matrix": MATRIX,
                },
            },
            headers=AUTH,
        )
    assert r.status_code == 400
    assert "outside the 2-location cost matrix" in r.json()["error"]
    assert transport.calls == []


def test_an_unreachable_solver_is_502_not_500():
    def boom(method, url, body, headers, timeout):
        raise OSError("connection refused")

    client = CuoptClient("http://solver:5000", transport=boom)
    with _app(client) as c:
        r = c.post("/invoke", json={"tool": "solver_health"}, headers=AUTH)
    assert r.status_code == 502
    assert "unreachable" in r.json()["error"]


def test_a_solver_fault_is_logged_at_error_for_the_self_heal_rail(caplog):
    """Contract A turns failures into structured non-2xx responses, so there is no
    unhandled 5xx traceback for the OpenObserve `level=error` alert to key off.
    The rail has to be fired from the handler path or a down solver pages nobody.
    """
    client = _client_for(Response(500, {"detail": "solver crashed"}))
    with caplog.at_level(logging.ERROR, logger="gridiron.mcp.cuopt"):
        with pytest.raises(ToolError):
            dispatch("solver_health", {}, client=client)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a solver fault emitted no ERROR record"
    assert "solver_health" in errors[0].getMessage()


def test_a_caller_error_stays_at_warning(caplog):
    """A bad location index is the caller's fix. Logging it at ERROR would fire a
    self-heal incident for every malformed agent call and bury real faults."""
    with caplog.at_level(logging.DEBUG, logger="gridiron.mcp.cuopt"):
        with pytest.raises(ToolError):
            dispatch(
                "assign_fleet_tasks",
                {"robots": ROBOTS, "tasks": [{"id": "p", "location": 99}], "cost_matrix": MATRIX},
                client=_client_for(),
            )
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r for r in caplog.records if r.levelname == "WARNING"]


def test_unknown_tool_and_unknown_server_are_404():
    with _app(_client_for()) as c:
        assert c.post("/invoke", json={"tool": "nope"}, headers=AUTH).status_code == 404
        assert c.post(
            "/invoke", json={"server": "other", "tool": "solver_health"}, headers=AUTH
        ).status_code == 404


def test_non_json_body_is_400():
    with _app(_client_for()) as c:
        r = c.post(
            "/invoke", content=b"not json", headers={**AUTH, "Content-Type": "application/json"}
        )
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_idempotency_key_replays_instead_of_resolving():
    """A retried submit must not burn a second GPU solve."""
    transport = _fake_transport([Response(200, _SOLVED), Response(200, _SOLVED)])
    client = CuoptClient("http://solver:5000", transport=transport)
    body = {
        "tool": "assign_fleet_tasks",
        "arguments": {"robots": ROBOTS, "tasks": TASKS, "cost_matrix": MATRIX},
    }
    with _app(client) as c:
        first = c.post("/invoke", json=body, headers={**AUTH, "Idempotency-Key": "k1"})
        second = c.post("/invoke", json=body, headers={**AUTH, "Idempotency-Key": "k1"})
    assert first.json().get("replayed") is None
    assert second.json()["replayed"] is True
    assert second.json()["result"] == first.json()["result"]
    assert len(transport.calls) == 1, "the replay still called the solver"


def test_replay_is_tenant_scoped():
    """One tenant's cached solution must never answer another's call."""
    transport = _fake_transport([Response(200, _SOLVED), Response(200, {"reqId": "other"})])
    client = CuoptClient("http://solver:5000", transport=transport)
    body = {
        "tool": "assign_fleet_tasks",
        "arguments": {"robots": ROBOTS, "tasks": TASKS, "cost_matrix": MATRIX},
    }
    with _app(client) as c:
        c.post("/invoke", json=body, headers={**AUTH, "Idempotency-Key": "k", "X-Tenant-Id": "a"})
        second = c.post(
            "/invoke", json=body, headers={**AUTH, "Idempotency-Key": "k", "X-Tenant-Id": "b"}
        )
    assert second.json().get("replayed") is None
    assert len(transport.calls) == 2


def test_replay_cache_is_bounded():
    from gridiron.mcp.app import _ReplayCache

    cache = _ReplayCache(cap=3)
    for i in range(10):
        cache.put(("t", "tool", str(i)), i)
    assert len(cache._d) == 3
    assert cache.get(("t", "tool", "0")) is None
    assert cache.get(("t", "tool", "9")) == 9


# --------------------------------------------------------------------------- #
# The client's own contract
# --------------------------------------------------------------------------- #
def test_client_hits_the_documented_upstream_paths():
    transport = _fake_transport(
        [Response(200, {"reqId": "r"}), Response(200, {}), Response(200, {}), Response(200, {})]
    )
    client = CuoptClient("http://solver:5000/", transport=transport)
    client.submit({"a": 1})
    client.status("r")
    client.solution("r")
    client.cancel("r")
    assert [(c["method"], c["url"]) for c in transport.calls] == [
        ("POST", "http://solver:5000/cuopt/request"),
        ("GET", "http://solver:5000/cuopt/request/r"),
        ("GET", "http://solver:5000/cuopt/solution/r"),
        ("DELETE", "http://solver:5000/cuopt/request/r"),
    ]


def test_validation_only_is_sent_as_a_query_flag():
    transport = _fake_transport([Response(200, {})])
    CuoptClient("http://s:5000", transport=transport).submit({"a": 1}, validation_only=True)
    assert transport.calls[0]["url"].endswith("?validation_only=true")


def test_client_surfaces_a_solver_status_in_the_error():
    client = _client_for(Response(409, {"detail": "busy"}))
    with pytest.raises(CuoptError) as exc:
        client.status("r")
    assert exc.value.status == 409
    assert "409" in str(exc.value)


def test_lp_solution_without_vehicle_data_still_returns_the_raw_answer():
    """An LP/MILP result has no routes. Routing decode must degrade to None, not
    fail the fetch."""
    client = _client_for(Response(200, {"response": {"solver_response": {"status": 0}}}))
    out = dispatch("get_solve_result", {"request_id": "r"}, client=client)
    assert out["raw"]["response"]["solver_response"]["status"] == 0
    assert out["routing"]["assignments"] == []
