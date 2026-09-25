#!/usr/bin/env bash
# FUTURE (G4) deployment wrapper for the SESSION root. DISABLED BY DEFAULT and
# never invoked in G3. It exists so the shape of a safe apply is reviewed now:
#
#   1. the reviewed binding (config + price evidence + estimate + policy + plan
#      hashes) must verify against the plan file about to be applied;
#   2. the exact saved plan is applied, never a fresh one, so a different plan
#      cannot be applied under an old estimate;
#   3. the resulting resource ids are recorded in the ledger.
#
# Refuses unless BOTH G3_ALLOW_CLOUD_MUTATION=1 and --i-understand are given.
# `terraform apply` remains a human-approved command (CLAUDE.md).
set -euo pipefail

if [[ "${G3_ALLOW_CLOUD_MUTATION:-}" != "1" || "${1:-}" != "--i-understand" ]]; then
  echo "refused: cloud mutation is disabled (G3). Requires G3_ALLOW_CLOUD_MUTATION=1 and --i-understand." >&2
  exit 3
fi
shift
SESSION_ID="${1:?session id}"; PLAN_FILE="${2:?saved plan file (.tfplan)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TF_DIR="$ROOT/../../infrastructure/capabilities/aks-mlops/session"
: "${G3_SUBSCRIPTION_ID:?G3_SUBSCRIPTION_ID must name the target subscription}"

terraform -chdir="$TF_DIR" show -json "$PLAN_FILE" > "$PLAN_FILE.json"
python3 "$ROOT/scripts/g3/session.py" verify-binding --session-id "$SESSION_ID" --plan-json "$PLAN_FILE.json"
python3 "$ROOT/scripts/g3/session.py" admit --config "$ROOT/deploy/g3/session-config.json"

echo "would run: terraform -chdir=$TF_DIR apply -input=false $PLAN_FILE   (human-approved command)"
echo "then:      python3 scripts/g3/session.py resources --session-id $SESSION_ID <ids from terraform output>"
echo "G3: this wrapper stops here by design and applies nothing."
