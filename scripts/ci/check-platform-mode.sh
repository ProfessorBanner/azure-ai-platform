#!/usr/bin/env bash
#
# Refuse to deploy a product into an environment that is in platform IDLE mode.
#
# The environment's mode is the committed `default` of var.idle_mode in
# infrastructure/environments/<env>/platform_mode.tf — the same line Terraform
# CI/CD evaluates. While an environment is idle its Databricks compute has no
# NAT egress and no private path to the data lake, and a `databricks bundle
# deploy` could re-apply an UNPAUSED schedule. So every product CD pipeline
# runs this check before `bundle deploy`; it fails closed when the mode is
# idle or cannot be determined (docs/runbooks/platform-idle-mode.md, ADR 0013).
#
# Usage:  scripts/ci/check-platform-mode.sh <sandbox|dev|stg|prod>
# Exit:   0 active, 1 idle or undeterminable. Reads one file; no cloud access.
#
set -euo pipefail

env="${1:?environment required (sandbox|dev|stg|prod)}"
case "${env}" in sandbox | dev | stg | prod) ;; *) echo "ERROR: unknown environment '${env}'" >&2; exit 1 ;; esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mode_file="${repo_root}/infrastructure/environments/${env}/platform_mode.tf"
[[ -f "${mode_file}" ]] || { echo "ERROR: ${mode_file} not found" >&2; exit 1; }

# Exactly one uncommented `default = true|false` line is expected in this file.
# (bash 3.2 compatible: no mapfile.)
count="$(grep -cE '^[[:space:]]*default[[:space:]]*=[[:space:]]*(true|false)[[:space:]]*$' "${mode_file}" || true)"
if [[ "${count}" != "1" ]]; then
  echo "ERROR: expected exactly one 'default = true|false' in ${mode_file}, found ${count}" >&2
  exit 1
fi
mode_value="$(grep -E '^[[:space:]]*default[[:space:]]*=[[:space:]]*(true|false)[[:space:]]*$' "${mode_file}" | sed -E 's/^[[:space:]]*default[[:space:]]*=[[:space:]]*//; s/[[:space:]]*$//')"

case "${mode_value}" in
  false)
    echo "==> Platform mode for ${env}: ACTIVE (idle_mode default = false) — deployment may proceed."
    ;;
  true)
    echo "ERROR: environment '${env}' is in platform IDLE mode (idle_mode default = true in ${mode_file#"${repo_root}"/})." >&2
    echo "       Product deployments are blocked while idle: compute has no egress or data-lake path, and a bundle deploy" >&2
    echo "       could re-enable schedules. Restore the environment first (docs/runbooks/platform-idle-mode.md §6)." >&2
    exit 1
    ;;
esac
