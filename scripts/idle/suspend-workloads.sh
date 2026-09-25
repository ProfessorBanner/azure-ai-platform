#!/usr/bin/env bash
#
# Suspend Databricks workloads in one environment, driven by the ORIGINAL
# pre-suspension snapshot written by capture-workload-state.sh --kind original.
# Reversible actions only:
#   - jobs:       set schedule / trigger / continuous pause_status to PAUSED
#                 (only where the snapshot shows UNPAUSED); cancel active runs
#   - apps:       `apps stop` (compute deallocated, app definition retained)
#   - warehouses: `warehouses stop`
#   - clusters:   terminate all-purpose clusters (definition retained)
#
# It NEVER deletes jobs, apps, clusters, warehouses, serving endpoints,
# pipelines or data. Custom (non-foundation-model) serving endpoints have no
# reversible "stop"; they are only REPORTED — deleting one is a separate,
# explicit decision (docs/runbooks/platform-idle-mode.md).
#
# Safety:
#   - the snapshot path is REQUIRED (no implicit "latest");
#   - the snapshot must be kind=original, for this environment, taken from the
#     workspace the CLI is currently bound to; otherwise the script refuses;
#   - default is a DRY RUN that prints the exact commands. Pass --execute to act.
#
# Usage:
#   IDLE_DBX_PROFILE=aiplatform-<env> scripts/idle/suspend-workloads.sh <env> --snapshot tmp/idle/<env>/workload-state-original.json [--execute]
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
[[ "${kind}" == "original" ]] || fail "refusing to suspend from a '${kind}' snapshot; suspension must be driven by the kind=original baseline so restore has a single source of truth"

info "Suspending workloads for ${env} from ${snapshot} (execute=${execute})"

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
            paused = dict(block)
            paused["pause_status"] = "PAUSED"
            new_settings[key] = paused
    if new_settings:
        payload = json.dumps({"job_id": j["job_id"], "new_settings": new_settings})
        run(f"pause job {j['job_id']} ({j['name']}): {', '.join(new_settings)} -> PAUSED",
            "jobs", "update", "--json", payload)

for r in snap.get("active_runs", []):
    if r.get("run_id"):
        run(f"cancel active run {r['run_id']} of job {r.get('job_id')}", "jobs", "cancel-run", str(r["run_id"]))

for a in snap["apps"]:
    if a.get("compute_state") not in (None, "STOPPED", "DELETED"):
        run(f"stop app {a['name']} (compute {a['compute_state']})", "apps", "stop", a["name"])

for w in snap["warehouses"]:
    if w.get("state") not in ("STOPPED", "DELETED"):
        run(f"stop warehouse {w['id']} ({w['name']}, {w['state']})", "warehouses", "stop", w["id"])

for c in snap["clusters"]:
    if c.get("state") != "TERMINATED":
        run(f"terminate all-purpose cluster {c['cluster_id']} ({c['name']}, {c['state']})",
            "clusters", "delete", c["cluster_id"])

custom = [e for e in snap["serving_endpoints"] if not e.get("databricks_managed_foundation_model")]
if custom:
    print("NOTE  custom serving endpoints have no reversible stop and are NOT touched:")
    for e in custom:
        print(f"       - {e['name']} entities={e['entities']} (provisioned compute may bill while idle; deletion is a separate decision)")

if actions == 0:
    print("Nothing to suspend: no UNPAUSED schedules, active runs, running apps, warehouses or clusters in the snapshot.")
else:
    print(f"{'Applied' if execute else 'Planned'} {actions} action(s).")
PY
