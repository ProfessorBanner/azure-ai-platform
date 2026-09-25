#!/usr/bin/env bash
#
# Capture the CURRENT workload state of one environment's Databricks workspace,
# so restoration can resume exactly what was previously active — and nothing
# that was not.
#
# Read-only. Two kinds of snapshot, both written under tmp/idle/<env>/:
#
#   --kind original   (the pre-suspension baseline; REQUIRED once, before
#                      suspend-workloads.sh)
#       -> workload-state-original.json
#       Written ONCE. If it already exists the script refuses to overwrite it
#       (pass --force-original to replace it deliberately), so repeated runs
#       can never silently replace the state restore must return to.
#
#   --kind checkpoint (default; any later observation, e.g. post-suspend proof)
#       -> workload-state-checkpoint-<UTC timestamp>.json
#
# Every snapshot records the environment name and the workspace host it was
# taken from; suspend/restore verify both before acting.
#
# Contents per workspace:
#   jobs       id, name, schedule / trigger / continuous pause_status, compute
#   apps       name, compute state, app state
#   warehouses id, name, state, serverless, auto_stop
#   clusters   all-purpose (non-job) clusters with state and auto-termination
#   serving    endpoints, flagged when they are Databricks-managed foundation models
#   pipelines  id, name, state
#   active_runs currently running job runs
#
# Usage:
#   IDLE_DBX_PROFILE=aiplatform-<env> scripts/idle/capture-workload-state.sh <env> --kind original
#   IDLE_DBX_PROFILE=aiplatform-<env> scripts/idle/capture-workload-state.sh <env>            # checkpoint
#   (or IDLE_DBX_AUTH=azure-cli instead of a profile)
#
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env="${1:?environment required (sandbox|dev|stg|prod)}"
validate_env "${env}"
shift
kind="checkpoint"
force_original="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kind) kind="${2:?--kind needs original|checkpoint}"; shift 2 ;;
    --force-original) force_original="true"; shift ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ "${kind}" == "original" || "${kind}" == "checkpoint" ]] || fail "--kind must be original or checkpoint"

require_cmd databricks
require_cmd python3
databricks_env "${env}"
host="$(current_databricks_host)"

out_dir="$(env_tmp "${env}")"
mkdir -p "${out_dir}"
if [[ "${kind}" == "original" ]]; then
  out_file="${out_dir}/workload-state-original.json"
  if [[ -f "${out_file}" && "${force_original}" != "true" ]]; then
    fail "${out_file} already exists — it is the pre-suspension baseline and is preserved. Take a checkpoint instead, or pass --force-original to replace it deliberately."
  fi
else
  out_file="${out_dir}/workload-state-checkpoint-$(timestamp).json"
fi

info "Capturing ${kind} workload state for ${env} (${host}) -> ${out_file}"

python3 - "${env}" "${host}" "${kind}" "${out_file}" <<'PY'
import datetime as dt
import json
import subprocess
import sys

env, host, kind, out_file = sys.argv[1:5]


def cli(*args):
    r = subprocess.run(["databricks", *args, "-o", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"databricks {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else []


def pause(block):
    return (block or {}).get("pause_status")


snapshot = {
    "environment": env,
    "workspace_host": host,
    "kind": kind,
    "captured_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "captured_by": (cli("current-user", "me") or {}).get("userName"),
    "jobs": [], "apps": [], "warehouses": [], "clusters": [],
    "serving_endpoints": [], "pipelines": [], "active_runs": [],
}

for j in cli("jobs", "list"):
    full = cli("jobs", "get", str(j["job_id"]))
    s = full.get("settings", {})
    snapshot["jobs"].append({
        "job_id": j["job_id"],
        "name": s.get("name"),
        "schedule": s.get("schedule"),
        "trigger": s.get("trigger"),
        "continuous": s.get("continuous"),
        "schedule_pause_status": pause(s.get("schedule")),
        "trigger_pause_status": pause(s.get("trigger")),
        "continuous_pause_status": pause(s.get("continuous")),
        "run_as": full.get("run_as_user_name"),
    })

for a in cli("apps", "list"):
    snapshot["apps"].append({
        "name": a["name"],
        "compute_state": (a.get("compute_status") or {}).get("state"),
        "app_state": (a.get("app_status") or {}).get("state"),
    })

for w in cli("warehouses", "list"):
    snapshot["warehouses"].append({
        "id": w["id"], "name": w["name"], "state": w["state"],
        "serverless": w.get("enable_serverless_compute"), "auto_stop_mins": w.get("auto_stop_mins"),
    })

for c in cli("clusters", "list"):
    if c.get("cluster_source") == "JOB":
        continue  # ephemeral job clusters are not restorable state
    snapshot["clusters"].append({
        "cluster_id": c["cluster_id"], "name": c["cluster_name"], "state": c["state"],
        "source": c.get("cluster_source"), "autotermination_minutes": c.get("autotermination_minutes"),
    })

for e in cli("serving-endpoints", "list"):
    entities = [(se.get("entity_name") or se.get("name") or "") for se in (e.get("config") or {}).get("served_entities", [])]
    foundation = bool(entities) and all(x.startswith("system.ai.") for x in entities)
    snapshot["serving_endpoints"].append({
        "name": e["name"], "state": e.get("state"), "entities": entities,
        "databricks_managed_foundation_model": foundation,
    })

for p in cli("pipelines", "list-pipelines"):
    snapshot["pipelines"].append({"pipeline_id": p["pipeline_id"], "name": p["name"], "state": p.get("state")})

snapshot["active_runs"] = [{"run_id": r.get("run_id"), "job_id": r.get("job_id"), "state": r.get("state")}
                           for r in cli("jobs", "list-runs", "--active-only")]

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(snapshot, fh, indent=2, sort_keys=True)

active_sched = [j for j in snapshot["jobs"] if "UNPAUSED" in {j["schedule_pause_status"], j["trigger_pause_status"], j["continuous_pause_status"]}]
running_apps = [a for a in snapshot["apps"] if a["compute_state"] == "ACTIVE"]
running_wh = [w for w in snapshot["warehouses"] if w["state"] not in ("STOPPED", "DELETED")]
running_cl = [c for c in snapshot["clusters"] if c["state"] != "TERMINATED"]
custom_serving = [e for e in snapshot["serving_endpoints"] if not e["databricks_managed_foundation_model"]]

print(f"jobs: {len(snapshot['jobs'])} (with an UNPAUSED schedule/trigger: {len(active_sched)})")
for j in active_sched:
    print(f"  - {j['job_id']} {j['name']} schedule={j['schedule_pause_status']} trigger={j['trigger_pause_status']} continuous={j['continuous_pause_status']}")
print(f"apps: {len(snapshot['apps'])} (compute ACTIVE: {len(running_apps)})")
print(f"warehouses: {len(snapshot['warehouses'])} (not stopped: {len(running_wh)})")
print(f"all-purpose clusters: {len(snapshot['clusters'])} (not terminated: {len(running_cl)})")
print(f"serving endpoints: {len(snapshot['serving_endpoints'])} (custom, i.e. not Databricks foundation models: {len(custom_serving)})")
for e in custom_serving:
    print(f"  - {e['name']} entities={e['entities']}")
print(f"pipelines: {len(snapshot['pipelines'])}; active job runs: {len(snapshot['active_runs'])}")
PY

chmod 0600 "${out_file}"
info "Snapshot written: ${out_file}"
