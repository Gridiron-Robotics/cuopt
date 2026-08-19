# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request ownership for the cuOpt MCP surface: who created a ``reqId``, and
who is therefore allowed to poll or cancel it.

WHAT THIS CLOSES
----------------
Upstream cuOpt mints a ``reqId`` and attaches no owner to it. Every id-bearing
endpoint — ``GET /cuopt/request/{id}``, ``GET /cuopt/solution/{id}``,
``DELETE /cuopt/request/{id}`` — answers on the id alone. Before this module the
overlay forwarded them unchanged, so any holder of the MCP bearer token who
learned or guessed a request id could read another caller's solution or
``cancel_solve`` it. Cancelling is destructive: the request and its cached input
are dropped and the original caller's next poll 404s.

So: record the creator at submit, check the caller against it on every
id-bearing tool.

WHAT THE OWNER IS, AND WHY
--------------------------
The owner is a **principal**: the pair

    (credential fingerprint, asserted tenant)

serialised as ``"<sha256(bearer)[:16]>/<X-Tenant-Id>"``. Those are the only two
identity-bearing inputs this transport receives, and they are worth different
things:

* The **credential fingerprint** is the authenticated half. The caller proved
  possession of the bearer token, and the fingerprint is a stable name for
  whatever that token stands for. The raw token is never stored, logged or
  returned — only its digest, and only a prefix of it.
* The **asserted tenant** (``X-Tenant-Id``) is the *unauthenticated* half. It
  separates tenants, but any token holder can type any value into it.

Neither alone is the right thing to bind to. The credential alone is
authenticated but — with today's single shared deployment token — constant, so
it would separate deployments and not tenants, and every tenant behind one
gateway would own everything. The header alone separates tenants but is
forgeable by anyone already past the bearer check, so it would look like an
authorization boundary while being a naming convention. The pair is the best
available signal because it degrades in the right direction: today it is worth
exactly what the header is worth, and the day the estate issues one token per
tenant instead of one per deployment, the authenticated half becomes distinct
and the *same* code becomes a real boundary with no change here.

WHY THE ENFORCEMENT IS DEFAULT-OFF, AND WHAT STAYS UNPROVEN
-----------------------------------------------------------
``CUOPT_MCP_ENFORCE_REQUEST_OWNER`` defaults to ``false``. Stated plainly:

* **Proven, with the flag on.** A caller presenting a different principal than
  the creator is refused on ``get_solve_status`` / ``get_solve_result`` /
  ``cancel_solve``, before the solver is touched. That is a real defence against
  the failure this surface actually sees — an agent or gateway carrying a
  ``reqId`` from one tenant's context into another's — and against a destructive
  cross-tenant cancel by mistake.
* **NOT proven, and not claimable.** With one shared bearer this is not an
  authorization boundary against a *hostile* holder of that token: they can set
  ``X-Tenant-Id`` to the victim's value and present a matching principal. Making
  this an authorization boundary requires a per-tenant credential, which is
  estate work (token issuance), not overlay work. Until that exists, treat the
  flag as containment of accidents, not of adversaries.

Ownership is recorded whether or not the flag is set, so flipping it on does not
start from an empty map and deny live traffic.

STORAGE, AND THE TWO PRECONDITIONS FOR TURNING IT ON
-----------------------------------------------------
The map is an in-process bounded LRU, like the replay cache next to it, which
means:

1. **One worker, or a shared store.** A solve submitted through worker A has no
   record on worker B. When enforcing, an unrecorded id is REFUSED rather than
   waved through — a fail-open "unknown means allowed" would make the check
   bypassable by load-balancer luck, which is worse than not having it. So a
   multi-worker deployment must either pin sessions or replace
   :class:`OwnerRegistry` with a Redis-backed one before setting the flag. The
   failure mode if you forget is loud (every poll 403s), which is the point.
2. **Capacity.** ``MAX_OWNER_ENTRIES`` records are kept, LRU. A solve polled
   after 4096 other solves have been created is indistinguishable from an
   unknown id and is refused. Raise the cap, or move to a store with a TTL,
   for a deployment whose solves outlive that window.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections import OrderedDict

MAX_OWNER_ENTRIES = 4096

#: Env var gating enforcement. Default-off — see the module docstring.
ENFORCE_ENV = "CUOPT_MCP_ENFORCE_REQUEST_OWNER"

#: Argument name every id-bearing tool uses for the solver request id.
REQUEST_ID_ARG = "request_id"

UNKNOWN_REQUEST = (
    "request_id is not recorded as owned by any caller on this worker. Refusing "
    "to act on it: with request-owner enforcement on, an unrecorded id cannot be "
    "distinguished from another caller's. If solves are submitted through a "
    "different worker than they are polled from, pin sessions or back the owner "
    "registry with a shared store."
)
NOT_YOURS = (
    "request_id belongs to a different caller. Polling or cancelling another "
    "caller's solve is refused (CUOPT_MCP_ENFORCE_REQUEST_OWNER is on)."
)


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def enforcing(env: dict[str, str] | None = None) -> bool:
    """Whether ownership is enforced. Default ``False``."""
    env = env if env is not None else dict(os.environ)
    return _truthy(env.get(ENFORCE_ENV))


def principal(authorization: str | None, tenant: str | None) -> str:
    """The caller's identity: ``sha256(bearer)[:16] + "/" + asserted tenant``.

    The raw bearer never leaves this function. ``"-"`` stands in for either half
    when it is absent (an insecure-mode caller with no token, or no
    ``X-Tenant-Id``), so an anonymous caller still gets a stable principal rather
    than a wildcard that would match everyone.
    """
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    cred = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else "-"
    ten = (tenant or "").strip() or "-"
    return f"{cred}/{ten}"


def id_bearing_tools() -> frozenset[str]:
    """Tools that act on a caller-supplied ``request_id``, read from the catalog.

    Derived rather than listed, so a tool added later with a required
    ``request_id`` is enforced the day it is added instead of the day someone
    remembers to update a constant here.
    """
    from gridiron.mcp.tools import TOOLS

    return frozenset(
        t["name"]
        for t in TOOLS
        if REQUEST_ID_ARG in ((t.get("input_schema") or {}).get("required") or [])
    )


class OwnerRegistry:
    """``request_id -> principal``, bounded LRU, in-process.

    See the module docstring for why in-process is a stated precondition rather
    than an oversight.
    """

    def __init__(self, cap: int = MAX_OWNER_ENTRIES) -> None:
        self._d: OrderedDict[str, str] = OrderedDict()
        self._cap = cap

    def record(self, request_id: str, owner: str) -> None:
        rid = (request_id or "").strip()
        if not rid:
            return
        self._d[rid] = owner
        self._d.move_to_end(rid)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def owner_of(self, request_id: str) -> str | None:
        rid = (request_id or "").strip()
        if rid in self._d:
            self._d.move_to_end(rid)
            return self._d[rid]
        return None

    def __len__(self) -> int:
        return len(self._d)


def refusal(
    tool: str,
    arguments: dict[str, object],
    owner: str,
    registry: OwnerRegistry,
    *,
    enabled: bool,
) -> str | None:
    """``None`` when the call is allowed, else the message to return with 403.

    Deliberately says nothing about who the real owner is: the refusal must not
    become an oracle for enumerating other tenants.
    """
    if not enabled or tool not in id_bearing_tools():
        return None
    raw = arguments.get(REQUEST_ID_ARG)
    if not isinstance(raw, str) or not raw.strip():
        # Missing/blank ids are the schema's problem; validate_arguments raises
        # 422 for them. Refusing here would turn a malformed call into a 403 and
        # hide the real error from the caller.
        return None
    recorded = registry.owner_of(raw)
    if recorded is None:
        return UNKNOWN_REQUEST
    if not hmac.compare_digest(recorded, owner):
        return NOT_YOURS
    return None


def record_result(
    tool: str, result: object, owner: str, registry: OwnerRegistry
) -> None:
    """Record ownership of any ``request_id`` a tool result carries.

    Keyed off the RESULT rather than a list of creating tool names, so every
    path that mints an id — ``submit_cuopt_problem``, ``assign_fleet_tasks``, and
    anything added later — is recorded without a second place to keep in sync.

    Id-bearing tools are excluded, because the id in their result came FROM the
    caller and was checked above. ``cancel_solve`` echoes its argument back; were
    it treated as a creation, a successful cancel would re-home the request to
    whoever cancelled it.
    """
    if tool in id_bearing_tools() or not isinstance(result, dict):
        return
    rid = result.get(REQUEST_ID_ARG)
    if isinstance(rid, str) and rid.strip():
        registry.record(rid, owner)


__all__ = [
    "ENFORCE_ENV",
    "MAX_OWNER_ENTRIES",
    "NOT_YOURS",
    "REQUEST_ID_ARG",
    "UNKNOWN_REQUEST",
    "OwnerRegistry",
    "enforcing",
    "id_bearing_tools",
    "principal",
    "record_result",
    "refusal",
]
