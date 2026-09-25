#!/usr/bin/env bash
#
# Lightweight destructive-change guard for the Terraform CD pipeline.
#
# Reads the *structured JSON* form of a saved binary plan and fails the run if
# any resource change would delete (or replace) infrastructure. The current
# controlled-CD stage expects a **no-op** apply, so any delete/replace is
# treated as an unexpected, high-risk change that a human must investigate
# before it can proceed.
#
# SCOPE / LIMITATIONS (read before trusting this):
#   This is a deliberately small *lab guardrail*, NOT a production policy
#   engine. It inspects `terraform show -json`'s `resource_changes[].change.
#   actions` for the "delete" action only. It does not evaluate cost, data
#   sensitivity, drift semantics, module provenance, or provider-specific
#   force-replacement subtleties. A real environment should use a dedicated
#   policy-as-code tool (OPA/Conftest, Sentinel, or `terraform plan` policy
#   checks) in addition to human approval. The Azure DevOps Environment
#   approval remains the authoritative human gate; this check only fails fast
#   on the obvious destructive case so the approver is never asked to approve a
#   silent deletion.
#
# IDLE MODE (docs/runbooks/platform-idle-mode.md):
#   Environment roots declare `var.idle_mode`. When the plan was produced with
#   idle_mode = true (the committed default of an environment in idle mode), a
#   fixed list of SIX exact resource addresses may be PURELY deleted: the NAT
#   gateway, its public-IP and subnet associations, and the two storage
#   private endpoints (see IDLE_DELETE_ALLOWLIST below). Every other delete —
#   including other resources of those same types — and EVERY replacement
#   still fails the run. When idle_mode is false or absent the behaviour is
#   exactly as before: any delete or replace fails.
#
# It performs NO Azure or state access: `terraform show -json` decodes a plan
# file offline (the working directory must already be initialised so provider
# schemas are available). It never runs apply or destroy.
#
# Arguments:
#   $1  working directory of the Terraform configuration (e.g. "bootstrap")
#   $2  binary plan file, relative to -chdir or absolute (e.g. "terraform.tfplan"),
#       OR an already-rendered `terraform show -json` file ending in ".json"
#       (used by tests/ci/test-check-destructive-plan.sh; no terraform needed).
#
set -euo pipefail

WORKING_DIR="${1:?working directory required (arg 1)}"
PLAN_FILE="${2:?binary plan file required (arg 2)}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"

if [[ "${PLAN_FILE}" == *.json ]]; then
  # Pre-rendered plan JSON (test path): inspect it directly, no terraform call.
  plan_json_file="${PLAN_FILE}"
  [[ -f "${plan_json_file}" ]] || fail "plan JSON not found: ${plan_json_file}"
  echo "==> Inspecting pre-rendered plan JSON ${plan_json_file}"
else
  command -v terraform >/dev/null 2>&1 || fail "terraform not found on PATH"
  echo "==> Rendering structured plan JSON for destructive-change inspection"
  # Write the structured plan JSON to a secure temp file and pass its PATH to
  # Python. It must NOT be piped into `python3 - <<'PY'`: the heredoc already
  # occupies Python's stdin (it is the program source read via `-`), so the pipe
  # is discarded and `json.load(sys.stdin)` sees exhausted stdin and fails with
  # `JSONDecodeError: Expecting value: line 1 column 1`.
  plan_json_file="$(mktemp)" || fail "could not create temp file for plan JSON"
  trap 'rm -f "${plan_json_file}"' EXIT
  terraform -chdir="${WORKING_DIR}" show -json "${PLAN_FILE}" > "${plan_json_file}" \
    || fail "terraform show -json failed for ${PLAN_FILE}"
fi

# Parse the JSON with python3 (no jq dependency). Fail on any resource change
# whose action set includes "delete" (covers pure deletes and replacements,
# which Terraform encodes as ["delete","create"] or ["create","delete"]).
python3 - "${plan_json_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as plan_fh:
    data = json.load(plan_fh)
changes = data.get("resource_changes", [])

# Idle mode is read from the plan itself (root variables are recorded in the
# plan JSON), so this guard follows the committed environment mode and needs
# no pipeline parameter. A committed default is recorded as JSON true; a
# `-var idle_mode=true` override is recorded as the raw string "true". Both
# mean idle; anything else means active.
variables = data.get("variables") or {}
idle_value = (variables.get("idle_mode") or {}).get("value")
idle_mode = idle_value is True or (isinstance(idle_value, str) and idle_value.strip().lower() == "true")

# The ONLY changes idle mode may make: a PURE delete (actions == ["delete"])
# of exactly these six resource addresses. Any other address — including
# another resource of the same type, the same type in another module, or one
# of these at a different index — and any replacement is still refused.
IDLE_DELETE_ALLOWLIST = {
    "module.network.azurerm_nat_gateway.databricks[0]",
    "module.network.azurerm_nat_gateway_public_ip_association.databricks[0]",
    "module.network.azurerm_subnet_nat_gateway_association.host[0]",
    "module.network.azurerm_subnet_nat_gateway_association.container[0]",
    "module.storage_private_access.azurerm_private_endpoint.dfs[0]",
    "module.storage_private_access.azurerm_private_endpoint.blob[0]",
}


def idle_delete_allowed(address, actions):
    return address in IDLE_DELETE_ALLOWLIST and actions == ["delete"]


destructive = []
expected_idle = []
summary = {"create": 0, "update": 0, "delete": 0, "replace": 0, "no-op": 0}

for rc in changes:
    actions = rc.get("change", {}).get("actions", [])
    addr = rc.get("address", "<unknown>")
    if "delete" in actions and "create" in actions:
        summary["replace"] += 1
        destructive.append((addr, "replace", actions))
    elif "delete" in actions:
        summary["delete"] += 1
        if idle_mode and idle_delete_allowed(addr, actions):
            expected_idle.append(addr)
        else:
            destructive.append((addr, "delete", actions))
    elif "create" in actions:
        summary["create"] += 1
    elif "update" in actions:
        summary["update"] += 1
    else:
        summary["no-op"] += 1

print(
    "==> Plan action summary: "
    + ", ".join(f"{k}={v}" for k, v in summary.items())
    + f" (idle_mode={'true' if idle_mode else 'false'})"
)

if expected_idle:
    print("==> Expected IDLE-MODE removals (allowed by var.idle_mode = true):")
    for addr in expected_idle:
        print(f"  - {addr}: delete")

if destructive:
    print("ERROR: destructive change(s) detected in the plan:", file=sys.stderr)
    for addr, kind, actions in destructive:
        print(f"  - {addr}: {kind} (actions={actions})", file=sys.stderr)
    print(
        "This controlled-CD stage expects a no-op apply (or, in idle mode, only "
        "the allow-listed networking removals). A delete or replace outside that "
        "scope must be reviewed by a human before it can proceed. Failing the run.",
        file=sys.stderr,
    )
    sys.exit(1)

if expected_idle:
    print("==> Only expected idle-mode removals found — guard passed.")
else:
    print("==> No destructive (delete/replace) changes found — guard passed.")
PY
