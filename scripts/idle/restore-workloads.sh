#!/usr/bin/env bash
#
# Restore Databricks workloads in one environment to the state recorded in the
# ORIGINAL pre-suspension snapshot. Only what was previously active is resumed:
#   - jobs:       schedule / trigger / continuous set back to UNPAUSED only where
#                 the snapshot recorded UNPAUSED
#   - apps:       `apps start` only for apps whose compute was ACTIVE
#   - warehouses / clusters: NOT started (serverless warehouses start on demand);
#                 the snapshot is printed so the operator can start one by hand
#
# Run this AFTER the networking has been restored to ACTIVE mode and verified
# (scripts/idle/verify-network-mode.sh <env> active); classic compute cannot
# egress or reach the data lake before that.
#
# Safety:
#   - the snapshot path is REQUIRED and must be kind=original, for this
#     environment, from the workspace the CLI is bound to; otherwise refuses;
#   - default is a DRY RUN. Pass --execute to act.
#
# Usage:
#   IDLE_DBX_PROFILE=aiplatform-<env> scripts/idle/restore-workloads.sh <env> --snapshot tmp/idle/<env>/workload-state-original.json [--execute]
#
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env="${1:?environment required (sandbox|dev|stg|prod)}"
validate_env "${env}"
shift
snapshot=""
execute="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) snapshot="${2:?--snapshot needs a path}"; shift 2 ;;
    --execute) execute="true"; shift ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ -n "${snapshot}" ]] || fail "--snapshot <path> is required (use the ORIGINAL snapshot: tmp/idle/${env}/workload-state-original.json)"
[[ -f "${snapshot}" ]] || fail "snapshot not found: ${snapshot}"

require_cmd databricks
require_cmd python3
databricks_env "${env}"
host="$(current_databricks_host)"
assert_snapshot_identity "${snapshot}" "${env}" "${host}"
kind="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("kind",""))' "${snapshot}")"
[[ "${kind}" == "original" ]] || fail "refusing to restore from a '${kind}' snapshot; only the kind=original baseline describes what was active before suspension"

info "Restoring workloads for ${env} from ${snapshot} (execute=${execute})"

python3 - "${snapshot}" "${execute}" <<'PY'
import json
import subprocess
import sys

snapshot_file, execute = sys.argv[1], sys.argv[2] == "true"
with open(snapshot_file, encoding="utf-8") as fh:
    snap = json.load(fh)

actions = 0


def run(desc, *args):
    global actions
    actions += 1
    print(f"{'EXEC ' if execute else 'DRY  '} {desc}")
    print(f"       databricks {' '.join(args)}")
    if execute:
        r = subprocess.run(["databricks", *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"FAILED: {r.stderr.strip()}")


for j in snap["jobs"]:
    new_settings = {}
    for key in ("schedule", "trigger", "continuous"):
        block = j.get(key)
        if block and block.get("pause_status") == "UNPAUSED":
            new_settings[key] = dict(block)  # exactly as captured, i.e. UNPAUSED
    if new_settings:
        payload = json.dumps({"job_id": j["job_id"], "new_settings": new_settings})
        run(f"unpause job {j['job_id']} ({j['name']}): {', '.join(new_settings)} -> UNPAUSED (as before)",
            "jobs", "update", "--json", payload)

for a in snap["apps"]:
    if a.get("compute_state") == "ACTIVE":
        run(f"start app {a['name']} (was ACTIVE)", "apps", "start", a["name"])

for w in snap["warehouses"]:
    print(f"INFO  warehouse {w['id']} ({w['name']}) was {w['state']} at capture; serverless warehouses start on demand — not started here.")
for c in snap["clusters"]:
    print(f"INFO  all-purpose cluster {c['cluster_id']} ({c['name']}) was {c['state']} at capture — not started here (start manually if needed).")

if actions == 0:
    print("Nothing to restore: the snapshot recorded no UNPAUSED schedules and no running apps.")
else:
    print(f"{'Applied' if execute else 'Planned'} {actions} action(s).")
PY
