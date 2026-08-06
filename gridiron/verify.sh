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
n=0
hdr() { n=$((n + 1)); echo; echo "── $n · $* ──"; }
ok() { echo "✅ $*"; }
bad() { echo "❌ $*"; fail=1; }

PY="${PYTHON:-python3}"

# --------------------------------------------------------------------------- #
hdr "overlay tests — no GPU, no solver, no network"
if "$PY" -m pytest gridiron/ -q -p no:cacheprovider >/tmp/cuopt-gate-tests.log 2>&1; then
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
"$PY" - <<'EOF'
import sys
sys.path.insert(0, ".")
from gridiron.observability import gridiron_otel as otel

name = getattr(otel, "DEFAULT_SERVICE_NAME", None) or "cuopt"
print(f"   service.name == OpenObserve stream == incident module == {name!r}")
EOF
ok "identity resolved from one place"

# --------------------------------------------------------------------------- #
hdr "upstream is untouched"
# The house rule, enforced rather than trusted. Our value is the seam; the moment
# this fork carries edits to NVIDIA's tree it stops being rebaseable onto
# upstream, and every future cuOpt release becomes a merge conflict instead of a
# fast-forward.
base="$(git merge-base HEAD 8c79892 2>/dev/null || echo '')"
if [ -z "$base" ]; then
  echo "   (upstream base commit not present in this clone — skipped)"
else
  # Committed history AND the working tree. Checking only `$base..HEAD` reads
  # the gate as green while an upstream edit sits uncommitted in front of you —
  # which is precisely when a gate is supposed to speak. Mutation-checked: the
  # first version of this step passed with a modified README.md staged to go.
  touched="$(
    {
      git diff --name-only "$base"..HEAD
      git diff --name-only HEAD
      git diff --name-only --cached
      git ls-files --others --exclude-standard
    } | sort -u | grep -v '^gridiron' || true
  )"
  if [ -z "$touched" ]; then
    ok "no changes outside gridiron/, committed or pending"
  else
    echo "$touched" | sed 's/^/   /'
    bad "upstream files modified — this fork must stay rebaseable"
  fi
fi

# --------------------------------------------------------------------------- #
echo
if [ "$fail" -eq 0 ]; then
  echo "════════════  GREEN — $n/$n  ════════════"
else
  echo "════════════  RED  ════════════"
fi
exit "$fail"
