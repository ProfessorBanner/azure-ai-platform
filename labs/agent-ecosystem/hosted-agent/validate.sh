#!/usr/bin/env bash
# Local validation for the Hosted Agent package. No Azure, no deployment.
#
#   ./validate.sh
#
# Proves the package is deployable-shaped and that the Phase 18 control model
# survived hosting. Everything here runs offline against the deterministic fake.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> format"
uv run ruff format --check .

echo "==> lint"
uv run ruff check .

echo "==> types (strict)"
uv run mypy src tests

echo "==> tests"
uv run pytest -q

echo "==> the official server starts, is ready, and holds the approval"
uv run python scripts/local_smoke.py

# THE ARTEFACT, NOT THE WORKSTATION.
#
# Everything above runs against this machine's virtualenv, where `hosted_agent`
# is importable by construction. That is exactly why the 19.1e image could pass
# every check here and still fail every Foundry session with `No module named
# hosted_agent`. The container check is the one that runs the thing that ships.
#
# Skipped rather than failed when Docker is unavailable: this script must stay
# runnable on a machine without a daemon, and the omission is stated loudly
# instead of passing quietly.
echo "==> the built container starts under its declared CMD and serves /readiness"
if docker info >/dev/null 2>&1; then
  ./scripts/container_smoke.sh
else
  echo "   SKIPPED - no Docker daemon. The image was NOT validated."
  echo "   Run ./scripts/container_smoke.sh before building any image you intend to push."
fi

echo
echo "OK - package validated locally. Nothing was deployed and no Azure call was made."
