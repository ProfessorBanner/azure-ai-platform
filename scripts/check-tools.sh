#!/usr/bin/env bash

set -euo pipefail

required_commands=(
  git
  gh
  az
  terraform
  tflint
  trivy
  pre-commit
  uv
  databricks
  jq
  yq
)

missing=0

for command_name in "${required_commands[@]}"; do
  if command -v "${command_name}" >/dev/null 2>&1; then
    printf "✓ %-15s %s\n" "${command_name}" "$(command -v "${command_name}")"
  else
    printf "✗ %-15s missing\n" "${command_name}"
    missing=1
  fi
done

if ! command -v docker >/dev/null 2>&1; then
  printf "⚠ %-15s not found; install or start Docker Desktop\n" "docker"
fi

if [[ "${missing}" -ne 0 ]]; then
  echo
  echo "One or more required tools are missing."
  exit 1
fi

echo
echo "Foundation tools are available."
