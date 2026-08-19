# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request ownership on the cuOpt MCP surface.

Upstream cuOpt answers ``/cuopt/request/{id}`` on the id alone, so before
:mod:`gridiron.mcp.ownership` any holder of the bearer token who learned a
``reqId`` could poll another caller's solution or ``cancel_solve`` it. These
tests drive the real router over HTTP — never the guard function on its own,
because a guard that works and a route that calls it are two different claims,
and only the second one protects anything.

Every id-bearing tool is enumerated FROM SOURCE (the catalog's ``required``
lists) and cross-checked against an independent description of the same surface
(the client methods that interpolate an id into a solver URL path). The
enumeration carries a hard floor at import time: an enumerator that finds
nothing must fail collection, not report a green file of zero tests.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from gridiron.mcp import ownership
from gridiron.mcp.app import build_app
from gridiron.mcp.client import CuoptClient, Response
from gridiron.mcp.ownership import OwnerRegistry, id_bearing_tools, principal
from gridiron.mcp.tools import TOOLS, dispatch

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ENFORCING = {"CUOPT_MCP_TOKEN": TOKEN, "CUOPT_MCP_ENFORCE_REQUEST_OWNER": "true"}
PERMISSIVE = {"CUOPT_MCP_TOKEN": TOKEN}
# Insecure mode accepts any Authorization header, which is the only way to put
# two DIFFERENT credentials through one deployment today. It is how the
# forward-looking half of the design — per-tenant tokens — is exercised.
INSECURE_ENFORCING = {
    "CUOPT_MCP_ALLOW_INSECURE": "true",
    "CUOPT_MCP_ENFORCE_REQUEST_OWNER": "true",
}

ROBOTS = [{"id": "amr-1", "location": 0}]
TASKS = [{"id": "pick-A", "location": 1}]
MATRIX = [[0, 4], [4, 0]]


def _fake_transport(responses):
    calls = []

    def transport(method, url, body, headers, timeout):
        calls.append({"method": method, "url": url, "body": body})
        return responses.pop(0) if responses else Response(200, {})

    transport.calls = calls
    return transport


def _app(*responses, env=ENFORCING):
    """A TestClient over the real app plus the transport it recorded through."""
    transport = _fake_transport(list(responses))
    client = CuoptClient("http://solver:5000", transport=transport)
    return TestClient(build_app(client=client, env=env)), transport


def _submit(client, headers, tool="submit_cuopt_problem"):
    """Create a solve as ``headers``' principal; return its request id."""
    args = (
        {"problem": {"a": 1}}
        if tool == "submit_cuopt_problem"
        else {"robots": ROBOTS, "tasks": TASKS, "cost_matrix": MATRIX}
    )
    r = client.post("/invoke", json={"tool": tool, "arguments": args}, headers=headers)
    assert r.status_code == 200, r.text
    rid = r.json()["result"]["request_id"]
    assert rid, "submit returned no request id to own"
    return rid


def _poll_args(tool, request_id):
    return {"request_id": request_id}


# --------------------------------------------------------------------------- #
# The enumerator, and the floor that stops it silently finding nothing.
# --------------------------------------------------------------------------- #
_ID_BEARING = sorted(id_bearing_tools())
assert len(_ID_BEARING) >= 3, (
    "the id-bearing tool enumerator found "
    f"{_ID_BEARING} — the cuOpt catalog has at least get_solve_status, "
    "get_solve_result and cancel_solve. A sweep that walks an empty set "
    "reports green having tested nothing; failing collection here is the point."
)


def _minting_tools():
    """Which catalog tools MINT a new request id, determined by running them.

    Behavioural, not a hand-kept list: each tool is dispatched against a stub
    solver that answers ``reqId=probe-minted`` while every id-bearing tool is
    handed ``probe-supplied``. A tool that comes back carrying the solver's id
    created a solve; one that echoes the caller's id (``cancel_solve``) did not.
    """
    probe_args = {
        "assign_fleet_tasks": {"robots": ROBOTS, "tasks": TASKS, "cost_matrix": MATRIX},
        "submit_cuopt_problem": {"problem": {"a": 1}},
        "get_solve_status": {"request_id": "probe-supplied"},
        "get_solve_result": {"request_id": "probe-supplied"},
        "cancel_solve": {"request_id": "probe-supplied"},
        "solver_health": {},
    }
    minted = set()
    for spec in TOOLS:
        name = spec["name"]
        assert name in probe_args, (
            f"catalog tool {name!r} has no probe arguments here, so this "
            "enumerator cannot see it — add them rather than let the sweep "
            "quietly skip a tool"
        )
        client = CuoptClient(
            "http://solver:5000",
            transport=_fake_transport([Response(200, {"reqId": "probe-minted"})] * 4),
        )
        try:
            out = dispatch(name, probe_args[name], client=client)
        except Exception:
            continue
        if isinstance(out, dict) and out.get("request_id") == "probe-minted":
            minted.add(name)
    return sorted(minted)


_MINTING = _minting_tools()
assert len(_MINTING) >= 2, (
    f"the solve-creating enumerator found {_MINTING} — both "
    "submit_cuopt_problem and assign_fleet_tasks create an owned solve. "
    "An empty or short sweep must fail loudly, not pass quietly."
)


def test_the_enumerator_sees_the_whole_id_bearing_surface():
    """Cross-check the catalog-derived set against an independent description.

    The two descriptions are built from different files and different facts: the
    tool catalog's ``required`` lists (``tools.py``), and the client methods that
    interpolate a caller-supplied id into a solver URL path — the ones that call
    ``validate_request_id`` (``client.py``). If they disagree, one of them is
    blind and the sweep below is walking a subset.
    """
    assert set(_ID_BEARING) == {"get_solve_status", "get_solve_result", "cancel_solve"}

    interpolating = {
        name
        for name, fn in vars(CuoptClient).items()
        if callable(fn)
        and not name.startswith("__")
        and "validate_request_id" in inspect.getsource(fn)
    }
    assert interpolating == {"status", "solution", "cancel"}, interpolating
    assert len(interpolating) == len(_ID_BEARING), (
        f"catalog says {len(_ID_BEARING)} id-bearing tools "
        f"{_ID_BEARING}, the client exposes {len(interpolating)} id-bearing "
        f"methods {sorted(interpolating)} — one of the two is not seeing the "
        "whole surface"
    )


def test_the_minting_enumerator_matches_the_catalog():
    assert _MINTING == ["assign_fleet_tasks", "submit_cuopt_problem"]
    # cancel_solve echoes the caller's id back; it must not be mistaken for a
    # creator, or cancelling someone else's solve would re-home it to you.
    assert "cancel_solve" not in _MINTING


# --------------------------------------------------------------------------- #
# Enforcement, per id-bearing tool, end-to-end over HTTP.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool", _ID_BEARING)
def test_another_principal_cannot_touch_a_solve_it_did_not_create(tool):
    """The refusal half: a different tenant is refused on every id-bearing tool."""
    client, transport = _app(
        Response(200, {"reqId": "req-owned"}), Response(200, {"ok": True})
    )
    with client as c:
        rid = _submit(c, {**AUTH, "X-Tenant-Id": "acme"})
        before = len(transport.calls)
        r = c.post(
            "/invoke",
            json={"tool": tool, "arguments": _poll_args(tool, rid)},
            headers={**AUTH, "X-Tenant-Id": "evil-corp"},
        )
    assert r.status_code == 403, r.text
    assert "different caller" in r.json()["error"]
    assert r.json()["tool"] == tool
    # The guard runs BEFORE dispatch: the solver must not have been contacted at
    # all. For cancel_solve this is the whole point — a check that fires after
    # the handler has already deleted the request refuses nothing.
    assert len(transport.calls) == before, (
        f"{tool} reached the solver despite being refused: "
        f"{transport.calls[before:]}"
    )


@pytest.mark.parametrize("tool", _ID_BEARING)
def test_the_owner_is_still_allowed(tool):
    """Anti-tautology. A suite that only proves refusals stays green if the
    guard denies everyone, which would take the whole tool surface offline."""
    client, transport = _app(
        Response(200, {"reqId": "req-owned"}),
        Response(200, {"status": "done", "reqId": "req-owned"}),
    )
    owner = {**AUTH, "X-Tenant-Id": "acme"}
    with client as c:
        rid = _submit(c, owner)
        before = len(transport.calls)
        r = c.post(
            "/invoke",
            json={"tool": tool, "arguments": _poll_args(tool, rid)},
            headers=owner,
        )
    assert r.status_code == 200, r.text
    assert len(transport.calls) == before + 1, (
        f"{tool} was allowed but never reached the solver"
    )


@pytest.mark.parametrize("tool", _ID_BEARING)
def test_an_unrecorded_request_id_is_refused_rather_than_waved_through(tool):
    """Fail-closed on an id this worker never saw.

    'Unknown means allowed' would make the check bypassable by load-balancer
    luck, which is worse than not having it — so the refusal names the
    single-worker/shared-store precondition instead of hiding it.
    """
    client, transport = _app(Response(200, {"ok": True}))
    with client as c:
        r = c.post(
            "/invoke",
            json={"tool": tool, "arguments": _poll_args(tool, "never-submitted")},
            headers={**AUTH, "X-Tenant-Id": "acme"},
        )
    assert r.status_code == 403, r.text
    assert "not recorded as owned" in r.json()["error"]
    assert transport.calls == [], "an unowned id still reached the solver"


@pytest.mark.parametrize("tool", _ID_BEARING)
def test_enforcement_is_default_off(tool):
    """The flag guards a check whose tenant half is caller-asserted, so it ships
    off. This pins that shipping default rather than assuming it."""
    assert ownership.enforcing({}) is False
    client, transport = _app(
        Response(200, {"reqId": "req-owned"}),
        Response(200, {"ok": True}),
        env=PERMISSIVE,
    )
    with client as c:
        rid = _submit(c, {**AUTH, "X-Tenant-Id": "acme"})
        r = c.post(
            "/invoke",
            json={"tool": tool, "arguments": _poll_args(tool, rid)},
            headers={**AUTH, "X-Tenant-Id": "evil-corp"},
        )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("tool", _MINTING)
def test_every_solve_creating_tool_records_its_owner(tool):
    """Both creators are covered, not just the obvious one: assign_fleet_tasks
    mints a reqId too, and an unrecorded creator is an unenforced one."""
    client, transport = _app(
        Response(200, {"reqId": "req-owned"}), Response(200, {"ok": True})
    )
    with client as c:
        rid = _submit(c, {**AUTH, "X-Tenant-Id": "acme"}, tool=tool)
        r = c.post(
            "/invoke",
            json={"tool": "get_solve_status", "arguments": {"request_id": rid}},
            headers={**AUTH, "X-Tenant-Id": "other"},
        )
    assert r.status_code == 403, f"{tool} created {rid} without recording an owner"


# --------------------------------------------------------------------------- #
# Ordering: the guard must run before both side effects and shortcuts.
# --------------------------------------------------------------------------- #
def test_a_replay_hit_cannot_shortcut_the_ownership_check():
    """Move the ownership check AFTER the idempotency early-return and this goes
    red — which is the ordering that matters.

    The replay cache is keyed by (principal, tool, Idempotency-Key) and NOT by
    the arguments, so a caller who already has a cached answer under some key can
    re-present that key with a different request_id. If the cached result were
    returned before the ownership check ran, the caller would get a 200 for a
    request it is not entitled to make and the guard would be decorative.
    """
    client, _ = _app(
        Response(200, {"reqId": "mine"}),
        Response(200, {"reqId": "theirs"}),
        Response(200, {"status": "done"}),
    )
    me = {**AUTH, "X-Tenant-Id": "acme"}
    them = {**AUTH, "X-Tenant-Id": "beta"}
    with client as c:
        mine = _submit(c, me)
        theirs = _submit(c, them)
        assert mine != theirs

        warm = c.post(
            "/invoke",
            json={"tool": "get_solve_status", "arguments": {"request_id": mine}},
            headers={**me, "Idempotency-Key": "k"},
        )
        assert warm.status_code == 200, warm.text

        # Same principal, same key, someone else's request id.
        r = c.post(
            "/invoke",
            json={"tool": "get_solve_status", "arguments": {"request_id": theirs}},
            headers={**me, "Idempotency-Key": "k"},
        )
    assert r.status_code == 403, (
        "a cached entry under this caller's own Idempotency-Key answered a "
        f"request for another caller's solve: {r.json()}"
    )
    assert r.json().get("replayed") is None


def test_a_refused_cancel_never_reaches_the_solver():
    """cancel_solve is the destructive one. Move the guard after dispatch and the
    request is already gone by the time the 403 is written."""
    client, transport = _app(
        Response(200, {"reqId": "req-owned"}), Response(200, {"deleted": True})
    )
    with client as c:
        rid = _submit(c, {**AUTH, "X-Tenant-Id": "acme"})
        r = c.post(
            "/invoke",
            json={"tool": "cancel_solve", "arguments": {"request_id": rid}},
            headers={**AUTH, "X-Tenant-Id": "evil-corp"},
        )
    assert r.status_code == 403
    assert [call["method"] for call in transport.calls] == ["POST"], (
        f"a DELETE reached the solver on a refused cancel: {transport.calls}"
    )


def test_the_owner_can_still_cancel_after_a_refusal():
    """The refused cancel must not have destroyed anything — the real owner's
    cancel still works and still reaches the solver."""
    client, transport = _app(
        Response(200, {"reqId": "req-owned"}),
        Response(200, {"deleted": True}),
    )
    owner = {**AUTH, "X-Tenant-Id": "acme"}
    with client as c:
        rid = _submit(c, owner)
        c.post(
            "/invoke",
            json={"tool": "cancel_solve", "arguments": {"request_id": rid}},
            headers={**AUTH, "X-Tenant-Id": "evil-corp"},
        )
        r = c.post(
            "/invoke",
            json={"tool": "cancel_solve", "arguments": {"request_id": rid}},
            headers=owner,
        )
    assert r.status_code == 200, r.text
    assert [call["method"] for call in transport.calls] == ["POST", "DELETE"]


# --------------------------------------------------------------------------- #
# What the principal is made of.
# --------------------------------------------------------------------------- #
def test_a_different_credential_is_a_different_owner():
    """The authenticated half of the principal is load-bearing.

    Today one shared token makes it constant — which is exactly why enforcement
    is default-off. This pins the behaviour the estate's per-tenant credentials
    will rely on: two different bearers are two different owners, with no code
    change here.
    """
    client, _ = _app(
        Response(200, {"reqId": "req-owned"}),
        Response(200, {"ok": True}),
        env=INSECURE_ENFORCING,
    )
    with client as c:
        rid = _submit(c, {"Authorization": "Bearer alpha", "X-Tenant-Id": "acme"})
        r = c.post(
            "/invoke",
            json={"tool": "get_solve_status", "arguments": {"request_id": rid}},
            # SAME asserted tenant, different credential.
            headers={"Authorization": "Bearer beta", "X-Tenant-Id": "acme"},
        )
    assert r.status_code == 403, (
        "a second credential asserting the same tenant inherited the first "
        "credential's solves"
    )


def test_the_same_credential_and_tenant_is_the_same_owner():
    assert principal("Bearer a", "acme") == principal("Bearer a", "acme")
    assert principal("Bearer a", "acme") != principal("Bearer b", "acme")
    assert principal("Bearer a", "acme") != principal("Bearer a", "beta")
    # Absent halves collapse to a stable placeholder, never to a wildcard that
    # would match every other caller.
    assert principal(None, None) == principal("", "")
    assert principal(None, None) != principal("Bearer a", None)


def test_the_principal_never_carries_the_raw_token():
    """It is stored in memory and used in log lines; the secret must not be."""
    p = principal("Bearer super-secret-token", "acme")
    assert "super-secret-token" not in p
    assert p.endswith("/acme")


def test_a_bare_token_and_a_bearer_prefixed_one_are_the_same_principal():
    assert principal("Bearer tok", "t") == principal("tok", "t")
    assert principal("  Bearer   tok  ", "t") == principal("tok", "t")


# --------------------------------------------------------------------------- #
# The registry itself.
# --------------------------------------------------------------------------- #
def test_the_owner_registry_is_bounded_and_lru():
    reg = OwnerRegistry(cap=3)
    for i in range(10):
        reg.record(f"r{i}", "p")
    assert len(reg) == 3
    assert reg.owner_of("r0") is None
    assert reg.owner_of("r9") == "p"


def test_reading_an_owner_refreshes_it_against_eviction():
    """A long-running solve that is polled must not age out under a burst of new
    submits — otherwise enforcement turns a healthy poll into a 403."""
    reg = OwnerRegistry(cap=3)
    reg.record("long-running", "p")
    for i in range(2):
        reg.record(f"r{i}", "p")
    assert reg.owner_of("long-running") == "p"  # refreshes it
    reg.record("new", "p")
    assert reg.owner_of("long-running") == "p"
    assert reg.owner_of("r0") is None


def test_a_blank_request_id_is_left_to_schema_validation():
    """Refusing a malformed call with 403 would hide the real 422 from the
    caller, so the guard declines to have an opinion on it."""
    reg = OwnerRegistry()
    for bad in ("", "   ", None, 7):
        assert (
            ownership.refusal(
                "get_solve_status", {"request_id": bad}, "p", reg, enabled=True
            )
            is None
        )
