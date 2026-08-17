#!/usr/bin/env bash
#
# The gate for the GRIDIRON OVERLAY only — `gridiron/`. Nothing here judges
# upstream NVIDIA cuOpt, and nothing here may.
#
# WHY THE SCOPE IS DRAWN THAT WAY. This repo is a fork whose house rule is that
# upstream stays unmodified; our value is the integration seam. Upstream has its
# own CI, its own conventions and its own build (C++/CUDA, a GPU, hours). A gate
# that tried to cover both would be unrunnable here and would blur the one line
# that keeps this fork rebaseable. So: `gridiron/` is ours, it is pure Python, it
# needs no GPU and no cuOpt runtime, and this script decides whether it is green.
#
#   ./gridiron/verify.sh
#
# The overlay is invisible when it breaks — that is the whole reason for a gate.
# A dead MCP surface answers an empty catalog rather than an error; a dead
# self-heal rail is silent by construction. Nothing downstream goes red.
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

fail=0
partial=0
n=0
hdr() { n=$((n + 1)); echo; echo "── $n · $* ──"; }
ok() { echo "✅ $*"; }
bad() { echo "❌ $*"; fail=1; }

PY="${PYTHON:-python3}"

# --------------------------------------------------------------------------- #
hdr "overlay tests — no GPU, no solver, no network"
# BOTH overlay directories. This ran `pytest gridiron/` alone and reported "92
# passed" as though that were the overlay, while gridiron-deploy/ held 14 tests
# the gate never executed — among them the one asserting the cuOpt port is not
# published on 0.0.0.0. Step 6 below already treats gridiron-deploy/ as overlay,
# so the two halves of this script disagreed about what the overlay is; a gate
# that tests a subset of what it claims to judge is the failure mode this whole
# script exists to prevent.
if "$PY" -m pytest gridiron/ gridiron-deploy/ -q -p no:cacheprovider >/tmp/cuopt-gate-tests.log 2>&1; then
  ok "$(grep -oE '[0-9]+ passed' /tmp/cuopt-gate-tests.log | tail -1)"
else
  bad "overlay suite red"
  tail -25 /tmp/cuopt-gate-tests.log
fi

# --------------------------------------------------------------------------- #
hdr "auth is fail-closed"
# The single most consequential line in the overlay. An open tool surface on a
# GPU solver is unmetered GPU time for anyone who can reach the port — and
# `submit_cuopt_problem` costs real money per call.
"$PY" - <<'EOF'
import sys
sys.path.insert(0, ".")
from gridiron.mcp.app import auth_mode

mode = auth_mode({})
if mode != "fail-closed":
    print(f"   FAIL: no token configured yields {mode!r}, not 'fail-closed'")
    sys.exit(1)
if auth_mode({"CUOPT_MCP_TOKEN": "t"}) != "token":
    print("   FAIL: a configured token is not honoured")
    sys.exit(1)
print("   unset token -> fail-closed; configured token -> enforced")
EOF
if [ $? -eq 0 ]; then ok "auth refuses to serve tools without a credential"; else bad "auth posture"; fi

# --------------------------------------------------------------------------- #
hdr "every tool declares its blast radius"
"$PY" - <<'EOF'
import sys
sys.path.insert(0, ".")
from gridiron.mcp.tools import TOOLS

problems = []
for t in TOOLS:
    if not t.get("name"):
        problems.append("a tool has no name")
        continue
    # snake_case: the platform's MCP client raises MCPError on `inputSchema`.
    if "input_schema" not in t:
        problems.append(f"{t['name']}: input_schema (snake_case) missing")
    ann = t.get("annotations") or {}
    if not isinstance(ann.get("destructiveHint"), bool):
        problems.append(f"{t['name']}: destructiveHint drives the estate HITL gate")

names = {t["name"] for t in TOOLS}
if "assign_fleet_tasks" not in names:
    problems.append("assign_fleet_tasks missing — the fleet VRP hook is the point")
if "cancel_solve" not in names:
    problems.append("cancel_solve missing")
else:
    cancel = next(t for t in TOOLS if t["name"] == "cancel_solve")
    if cancel["annotations"]["destructiveHint"] is not True:
        problems.append("cancel_solve must be destructive — it discards a paid-for solve")

for p in problems:
    print(f"   {p}")
sys.exit(1 if problems else 0)
EOF
if [ $? -eq 0 ]; then ok "all tools annotated; cancel_solve destructive"; else bad "tool catalog"; fi

# --------------------------------------------------------------------------- #
hdr "one service name — stream == incident module"
# This step used to read `getattr(otel, "DEFAULT_SERVICE_NAME", None) or "cuopt"`
# and then call ok() unconditionally. DEFAULT_SERVICE_NAME has never existed, so
# the getattr always fell through to the literal and the step asserted nothing —
# it printed 'cuopt' and passed with the module deleted. Assert the invariant
# that actually matters instead: the OTLP service.name (= the OpenObserve stream
# = the incident 'module') and the Contract-A server name are the same string.
# When they drift, an alert fires on one stream while the tool surface is
# registered under another name, and nobody correlates them.
"$PY" - <<'EOF'
import sys
sys.path.insert(0, ".")
from gridiron.mcp.tools import SERVER_NAME
from gridiron.observability.asgi import SERVICE_NAME

if not SERVICE_NAME or not SERVER_NAME:
    print("   FAIL: service identity is empty")
    sys.exit(1)
if SERVICE_NAME != SERVER_NAME:
    print(
        f"   FAIL: observability service.name {SERVICE_NAME!r} != MCP server "
        f"name {SERVER_NAME!r}; alerts and tools would land under two identities"
    )
    sys.exit(1)
print(f"   service.name == OpenObserve stream == MCP server == {SERVICE_NAME!r}")
EOF
if [ $? -eq 0 ]; then ok "identity resolved from one place"; else bad "service identity"; fi

# --------------------------------------------------------------------------- #
hdr "gridiron manifests pin exact versions"
# The estate rule, with ONE carve-out: the CUDA/RAPIDS stack is left as floors
# because those wheels are platform/CUDA-specific and a hard pin breaks installs
# on mismatched targets. That carve-out is an allow-list BY NAME below — never a
# pattern, because a pattern like "anything with cu in it" silently exempts
# whatever a future author happens to name that way.
"$PY" - <<'EOF'
import pathlib
import re
import sys

# Exact package names exempted from the == rule. Names, not patterns.
CUDA_RAPIDS_FLOORS = {
    "cuda-python", "cudf", "cugraph", "cuml", "cupy", "cupy-cuda11x",
    "cupy-cuda12x", "cuspatial", "dask-cuda", "libcudf", "libcuopt",
    "libraft", "librmm", "numba-cuda", "nvidia-cuda-runtime-cu12",
    "nvidia-cublas-cu12", "nvidia-curand-cu12", "nvidia-cusparse-cu12",
    "nvidia-cusolver-cu12", "pylibcudf", "pylibraft", "raft-dask", "rmm",
    "torch", "torchvision",
}

problems = []
checked = 0
for path in sorted(pathlib.Path(".").glob("gridiron*/**/requirements*.txt")):
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        checked += 1
        name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip().lower()
        if name in CUDA_RAPIDS_FLOORS:
            print(f"   carve-out (CUDA/RAPIDS, by name): {path}:{lineno} {line}")
            continue
        if "==" not in line:
            problems.append(f"{path}:{lineno} {line!r} is not pinned with ==")

if not checked:
    problems.append("no gridiron requirements were found to check — the step is inert")

for p in problems:
    print(f"   {p}")
print(f"   {checked} requirement line(s) checked")
sys.exit(1 if problems else 0)
EOF
if [ $? -eq 0 ]; then ok "every gridiron dep pinned, or named in the CUDA/RAPIDS carve-out"; else bad "dependency pins"; fi

# --------------------------------------------------------------------------- #
hdr "upstream is untouched"
# The house rule, enforced rather than trusted. Our value is the seam; the moment
# this fork carries edits to NVIDIA's tree it stops being rebaseable onto
# upstream, and every future cuOpt release becomes a merge conflict instead of a
# fast-forward.
# WHY THIS IS SPLIT IN TWO. The previous version resolved a base with
# `git merge-base HEAD 8c79892` and, when that failed, printed "skipped" and let
# the step count toward "GREEN — 5/5". This clone is a squashed single commit, so
# 8c79892 is not a valid object and the branch NEVER ran: the gate's headline
# house rule was inert while reporting a clean pass. Verified by mutation —
# appending a line to README.md still produced GREEN 5/5.
#
# So the half that needs no base now runs unconditionally and is the one that
# can fail, and a missing base degrades the history half explicitly instead of
# being laundered into the pass count.

# --- half 1: the working tree. Always runnable, no base required. ----------- #
# This is also the half that matters most in practice: it is what stands between
# an upstream edit and a commit.
pending="$(
  {
    git diff --name-only HEAD
    git diff --name-only --cached
    git ls-files --others --exclude-standard
  } | sort -u | grep -Ev '^(gridiron|gridiron-deploy)/' || true
)"
if [ -z "$pending" ]; then
  ok "working tree clean outside the overlay (gridiron/, gridiron-deploy/)"
else
  echo "$pending" | sed 's/^/   /'
  bad "upstream files modified — this fork must stay rebaseable"
fi

# --- half 2: committed history, when a base can be resolved. ---------------- #
base="$(git merge-base HEAD 8c79892 2>/dev/null || true)"
if [ -z "$base" ]; then
  base="$(git merge-base HEAD origin/main 2>/dev/null || true)"
fi
if [ -z "$base" ]; then
  # NOT a pass. Recorded so the summary cannot claim a clean n/n.
  partial=$((partial + 1))
  echo "   ⚠ committed history NOT verified: no upstream base commit in this"
  echo "     clone (shallow/squashed). Only the working tree was checked."
else
  hist="$(git diff --name-only "$base"..HEAD | grep -Ev '^(gridiron|gridiron-deploy)/' || true)"
  if [ -z "$hist" ]; then
    ok "no committed changes outside the overlay since $(git rev-parse --short "$base")"
  else
    echo "$hist" | sed 's/^/   /'
    bad "upstream files modified in committed history"
  fi
fi

# --------------------------------------------------------------------------- #
echo
if [ "$fail" -ne 0 ]; then
  echo "════════════  RED  ════════════"
elif [ "$partial" -ne 0 ]; then
  # A gate that cannot check something must say so in the banner, not bury it.
  echo "════════════  GREEN with $partial UNVERIFIED check(s) — $n steps  ════════════"
  echo "  (see the ⚠ above; this is not a clean pass)"
else
  echo "════════════  GREEN — $n/$n  ════════════"
fi
exit "$fail"
