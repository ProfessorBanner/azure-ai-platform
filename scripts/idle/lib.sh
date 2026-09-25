#!/usr/bin/env bash
#
# Shared helpers for the platform idle-mode scripts (scripts/idle/*.sh).
#
# Conventions
#   - Every script takes the environment name (sandbox|dev|stg|prod) first.
#   - Nothing here is ever printed that could be a secret: only resource names,
#     IDs, states and schedule metadata.
#   - Databricks calls go through the Databricks CLI. Auth is EITHER an explicit
#     CLI profile (--profile <name>, the repository convention for local work)
#     OR, when IDLE_DBX_AUTH=azure-cli, the operator's existing `az login`
#     session against the workspace host read from Terraform outputs. No PAT,
#     client secret or token is ever accepted or written.
#   - Snapshots and plans live under tmp/idle/<env>/ which is git-ignored (tmp/).
#
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "==> $*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 not found on PATH"
}

# validate_env <env>
validate_env() {
  case "${1:-}" in
    sandbox | dev | stg | prod) ;;
    *) fail "environment must be one of: sandbox dev stg prod (got '${1:-}')" ;;
  esac
}

# Repository paths for an environment.
env_root() { echo "${repo_root}/infrastructure/environments/$1"; }
env_backend() { echo "${repo_root}/backend/platform-$1.hcl"; }
env_tmp() { echo "${repo_root}/tmp/idle/$1"; }
env_rg() { echo "rg-aiplatform-$1"; }
env_storage_account() { echo "stexample$1data"; }
env_vnet() { echo "vnet-aiplatform-$1"; }
env_nat() { echo "nat-dbx-$1"; }
env_pip() { echo "pip-dbx-nat-$1"; }

# databricks_args <env>  -> prints the auth arguments for the Databricks CLI.
# Resolution order:
#   1. DATABRICKS_CONFIG_PROFILE / IDLE_DBX_PROFILE  -> --profile <name>
#   2. IDLE_DBX_AUTH=azure-cli                       -> host from Terraform
#      outputs (the root must already be initialised) + azure-cli auth type.
# Anything else fails closed so a profile is never picked implicitly.
databricks_env() {
  local env="$1"
  local profile="${IDLE_DBX_PROFILE:-${DATABRICKS_CONFIG_PROFILE:-}}"
  if [[ -n "${profile}" ]]; then
    export DATABRICKS_CONFIG_PROFILE="${profile}"
    unset DATABRICKS_HOST DATABRICKS_AUTH_TYPE
    info "Databricks auth: CLI profile '${profile}'"
    return
  fi
  if [[ "${IDLE_DBX_AUTH:-}" == "azure-cli" ]]; then
    local host
    host="$(terraform -chdir="$(env_root "${env}")" output -raw databricks_workspace_url 2>/dev/null)" \
      || fail "could not read databricks_workspace_url from Terraform outputs for ${env}; run terraform init for that root first"
    export DATABRICKS_HOST="https://${host}"
    export DATABRICKS_AUTH_TYPE="azure-cli"
    unset DATABRICKS_CONFIG_PROFILE
    info "Databricks auth: azure-cli against ${DATABRICKS_HOST}"
    return
  fi
  fail "choose Databricks auth explicitly: IDLE_DBX_PROFILE=aiplatform-${env} (CLI profile) or IDLE_DBX_AUTH=azure-cli"
}

timestamp() { date -u +%Y%m%dT%H%M%SZ; }

# current_databricks_host -> the workspace host the CLI is currently bound to
# (after databricks_env), normalised to "https://host" with no trailing slash.
current_databricks_host() {
  local host
  host="$(databricks auth describe -o json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("details") or {}).get("host") or d.get("host") or "")')"
  [[ -n "${host}" ]] || fail "could not determine the current Databricks workspace host (databricks auth describe)"
  host="${host%/}"
  [[ "${host}" == https://* ]] || host="https://${host}"
  echo "${host}"
}

# assert_snapshot_identity <snapshot.json> <env> <current host>
# Refuses a snapshot taken for another environment or another workspace.
assert_snapshot_identity() {
  local snapshot="$1" env="$2" host="$3"
  python3 - "${snapshot}" "${env}" "${host}" <<'PY'
import json, sys
snapshot, env, host = sys.argv[1:4]
with open(snapshot, encoding="utf-8") as fh:
    snap = json.load(fh)
problems = []
if snap.get("environment") != env:
    problems.append(f"snapshot environment is '{snap.get('environment')}', expected '{env}'")
if (snap.get("workspace_host") or "").rstrip("/") != host.rstrip("/"):
    problems.append(f"snapshot workspace_host is '{snap.get('workspace_host')}', current workspace is '{host}'")
if not snap.get("captured_at_utc") or "jobs" not in snap:
    problems.append("snapshot is missing captured_at_utc/jobs; not a capture-workload-state.sh file")
if problems:
    print("ERROR: snapshot identity check failed:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    sys.exit(1)
print(f"==> Snapshot identity OK: environment={env} workspace_host={host} captured_at_utc={snap['captured_at_utc']} kind={snap.get('kind','unknown')}")
PY
}
