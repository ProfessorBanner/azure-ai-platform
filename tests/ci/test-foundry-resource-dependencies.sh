#!/usr/bin/env bash
#
# Regression test for the Foundry create-ordering contract.
#
# Azure treats a Cognitive Services account as a single mutable resource: two
# concurrent child writes against it fail with
#
#   409 RequestConflict: Another operation is in progress on the resource
#
# Both the project and the model deployment reference only the ACCOUNT, so
# Terraform infers no ordering between them and would create them in parallel.
# The module therefore carries an explicit `depends_on` that serialises
#
#   cognitive account -> cognitive account project -> cognitive deployment
#
# This test asserts that chain from the module source, so the dependency cannot
# be dropped during a refactor and silently reintroduce a first-apply failure
# that only reproduces on a clean create.
#
# It is a STATIC test: it reads Terraform source and never contacts Azure.
#
# Run: tests/ci/test-foundry-resource-dependencies.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

python3 - <<'PY'
import re
import sys

MODULE = "infrastructure/modules/ai-foundry/main.tf"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


source = open(MODULE).read()


def resource_block(resource_type: str, name: str) -> str:
    """Return the body of a single top-level resource block."""
    pattern = rf'^resource "{re.escape(resource_type)}" "{re.escape(name)}" \{{\n(.*?)^}}'
    match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
    if match is None:
        failures.append(f"{MODULE}: resource {resource_type}.{name} not found")
        return ""
    return match.group(1)


account = resource_block("azurerm_cognitive_account", "this")
project = resource_block("azurerm_cognitive_account_project", "this")
deployment = resource_block("azurerm_cognitive_deployment", "this")

# --- Link 1: project -> account (implicit, via the account id reference) -----
check(
    "azurerm_cognitive_account.this.id" in project,
    f"{MODULE}: the project must reference azurerm_cognitive_account.this.id so "
    f"Terraform orders it after the account",
)

# --- Link 2: deployment -> project (EXPLICIT; this is the 409 fix) ----------
depends_on = re.search(r"depends_on\s*=\s*\[(.*?)\]", deployment, re.DOTALL)
check(
    depends_on is not None,
    f"{MODULE}: azurerm_cognitive_deployment.this must declare an explicit "
    f"depends_on. Without it Terraform creates the deployment and the project "
    f"concurrently against the same account, which Azure rejects with "
    f"409 RequestConflict on a clean apply.",
)
if depends_on is not None:
    referenced = {ref.strip().rstrip(",") for ref in depends_on.group(1).split() if ref.strip()}
    check(
        "azurerm_cognitive_account_project.this" in referenced,
        f"{MODULE}: azurerm_cognitive_deployment.this must depend_on "
        f"azurerm_cognitive_account_project.this; found {sorted(referenced)}",
    )

# The deployment must still be anchored to the account itself.
check(
    "azurerm_cognitive_account.this.id" in deployment,
    f"{MODULE}: the deployment must reference azurerm_cognitive_account.this.id",
)

# --- The fix must stay dependency-only --------------------------------------
# A `depends_on` changes graph ordering, not configuration. If someone "fixes"
# ordering by introducing lifecycle rules or timeouts instead, the plan stops
# being a no-op against deployed infrastructure, so reject that here.
for forbidden, why in (
    ("lifecycle", "lifecycle blocks change resource behaviour, not just ordering"),
    ("create_before_destroy", "replacement semantics are not an ordering fix"),
):
    check(
        forbidden not in deployment,
        f"{MODULE}: azurerm_cognitive_deployment.this must not use `{forbidden}` — {why}",
    )

# --- The account must not depend on its own children (cycle guard) -----------
check(
    "depends_on" not in account,
    f"{MODULE}: azurerm_cognitive_account.this must not declare depends_on",
)

if failures:
    print("FAIL: Foundry resource dependency contract violated", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    sys.exit(1)

print("OK: Foundry create ordering enforced.")
print("  azurerm_cognitive_account.this")
print("    -> azurerm_cognitive_account_project.this   (implicit: account id reference)")
print("    -> azurerm_cognitive_deployment.this        (explicit: depends_on project)")
PY
