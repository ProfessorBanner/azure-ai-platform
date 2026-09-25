#!/usr/bin/env bash
#
# Regression test for the Terraform CD stage-identifier contract.
#
# Azure DevOps stage identifiers accept only alphanumerics and underscores, but
# `env` legitimately carries hyphens for capability roots (e.g. foundry-sandbox).
# templates/terraform-cd-stages.yml therefore sanitises hyphens when GENERATING
# stage ids, while leaving display names, paths, artifacts, variables and
# Environment names untouched.
#
# This test pins both halves of that contract:
#   1. the four existing environments keep their PRE-EXISTING stage ids exactly
#      (a rename would silently break `dependsOnStages` and any saved run
#      history), and
#   2. hyphenated callers produce underscore-safe ids.
#
# It reads the real template and the real caller pipelines, so it fails if a
# stage id stops being derived through the sanitising expression, if the two
# `validate_plan` references drift apart, or if a caller introduces a new `env`
# value whose expected id is not recorded here.
#
# Run: tests/ci/test-cd-stage-identifiers.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

python3 - <<'PY'
import re
import sys

TEMPLATE = "azure-pipelines/templates/terraform-cd-stages.yml"
CALLERS = [
    "azure-pipelines/terraform-cd-platform.yml",
    "azure-pipelines/terraform-cd-foundry-sandbox.yml",
]

# The stage ids each caller's `env` value MUST produce. The four platform
# environments are listed with the ids they had BEFORE sanitisation was
# introduced, which is what makes this a regression test rather than a
# restatement of the implementation.
EXPECTED = {
    "sandbox":         ("validate_plan_sandbox",         "apply_sandbox"),
    "dev":             ("validate_plan_dev",             "apply_dev"),
    "stg":             ("validate_plan_stg",             "apply_stg"),
    "prod":            ("validate_plan_prod",            "apply_prod"),
    "foundry-sandbox": ("validate_plan_foundry_sandbox", "apply_foundry_sandbox"),
}

VALID_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


template = open(TEMPLATE).read()

# --- 1. The template must derive all three identifier sites via replace() ----
expected_expressions = {
    "validate_plan stage id":
        "- stage: ${{ format('validate_plan_{0}', replace(parameters.env, '-', '_')) }}",
    "apply stage id":
        "- stage: ${{ format('apply_{0}', replace(parameters.env, '-', '_')) }}",
    "apply dependsOn":
        "dependsOn: ${{ format('validate_plan_{0}', replace(parameters.env, '-', '_')) }}",
}
for label, expression in expected_expressions.items():
    check(
        template.count(expression) == 1,
        f"{TEMPLATE}: expected exactly one occurrence of the {label} expression:\n"
        f"    {expression}",
    )

# No identifier site may still interpolate the raw, unsanitised parameter.
for raw in ("- stage: validate_plan_${{ parameters.env }}",
            "- stage: apply_${{ parameters.env }}",
            "dependsOn: validate_plan_${{ parameters.env }}"):
    check(raw not in template, f"{TEMPLATE}: unsanitised identifier still present: {raw}")

# --- 2. Display names must keep the RAW value (no accidental sanitising) -----
check(
    "displayName: 'Validate & plan · ${{ parameters.env }} (plan-only WIF)'" in template,
    f"{TEMPLATE}: validate/plan displayName must keep the raw (hyphenated) env value",
)
check(
    "displayName: 'Approve & apply · ${{ parameters.env }} (apply WIF)'" in template,
    f"{TEMPLATE}: apply displayName must keep the raw (hyphenated) env value",
)
check(
    "replace(" not in template.split("stages:", 1)[0].replace(
        "replace(parameters.env, '-', '_')", ""
    ),
    f"{TEMPLATE}: unexpected replace() outside the stage-identifier expressions",
)


def sanitise(value: str) -> str:
    """Mirror of the template's replace(parameters.env, '-', '_')."""
    return value.replace("-", "_")


# --- 3. Every caller's env value must map to its expected stage ids ----------
seen: set[str] = set()
for caller in CALLERS:
    for env in re.findall(r"^\s+env:\s*(\S+)\s*$", open(caller).read(), re.MULTILINE):
        seen.add(env)
        if env not in EXPECTED:
            failures.append(
                f"{caller}: caller passes env '{env}' with no expected stage ids "
                f"recorded in this test; add them."
            )
            continue

        expected_validate, expected_apply = EXPECTED[env]
        actual_validate = f"validate_plan_{sanitise(env)}"
        actual_apply = f"apply_{sanitise(env)}"

        check(actual_validate == expected_validate,
              f"{caller}: env '{env}' -> '{actual_validate}', expected '{expected_validate}'")
        check(actual_apply == expected_apply,
              f"{caller}: env '{env}' -> '{actual_apply}', expected '{expected_apply}'")
        check(bool(VALID_IDENTIFIER.match(actual_validate)),
              f"{caller}: '{actual_validate}' is not a valid Azure DevOps stage identifier")
        check(bool(VALID_IDENTIFIER.match(actual_apply)),
              f"{caller}: '{actual_apply}' is not a valid Azure DevOps stage identifier")

missing = set(EXPECTED) - seen
check(not missing, f"expected env value(s) no longer passed by any caller: {sorted(missing)}")

# --- 4. Cross-stage dependsOnStages literals must name real stage ids --------
valid_ids = {i for env in seen for i in
             (f"validate_plan_{sanitise(env)}", f"apply_{sanitise(env)}")}
# Guard stages are defined by the caller itself, not generated by the template.
locally_defined = set()
for caller in CALLERS:
    locally_defined.update(re.findall(r"^\s+- stage:\s*([A-Za-z0-9_]+)\s*$",
                                      open(caller).read(), re.MULTILINE))

for caller in CALLERS:
    body = open(caller).read()
    for block in re.findall(r"dependsOnStages:\s*\n((?:\s+- \S+\n)+)", body):
        for ref in re.findall(r"- (\S+)", block):
            check(ref in valid_ids or ref in locally_defined,
                  f"{caller}: dependsOnStages references unknown stage '{ref}'")
    for inline in re.findall(r"dependsOnStages:\s*\[([^\]]*)\]", body):
        for ref in [r.strip() for r in inline.split(",") if r.strip()]:
            check(ref in valid_ids or ref in locally_defined,
                  f"{caller}: dependsOnStages references unknown stage '{ref}'")

if failures:
    print("FAIL: CD stage-identifier contract violated", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    sys.exit(1)

print("OK: CD stage identifiers valid and unchanged for existing environments.")
for env in sorted(seen):
    print(f"  {env:16} -> validate_plan_{sanitise(env)} , apply_{sanitise(env)}")
PY
