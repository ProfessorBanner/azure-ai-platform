#!/usr/bin/env bash
#
# Table-driven local tests for scripts/ci/verify-promotion.sh.
#
# The promotion gate is pure value validation, so its whole contract can be
# proven locally at L0 with no pipeline run, no Azure or Databricks call and no
# deployment. Each row supplies the six run-identity values the PROD pipeline
# passes through env:, and asserts BOTH the exit status and the message the gate
# produces — a gate that failed for the wrong reason is not a passing gate.
#
# The final wiring block asserts that azure-ai-platform's PROD pipeline actually
# invokes this script with the correct Azure Pipelines expressions. Tests that
# prove the script in isolation while the pipeline calls it with the wrong
# values would be a false negative, so the mapping is checked here too.
#
# Usage:  ./tests/ci/test-verify-promotion.sh
# Exit:   0 all tests passed, 1 one or more failed.
#
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
gate="${repo_root}/scripts/ci/verify-promotion.sh"
pipeline="${repo_root}/azure-pipelines/databricks-cd-prod.yml"

[[ -x "${gate}" ]] || { echo "FATAL: ${gate} missing or not executable"; exit 1; }
[[ -f "${pipeline}" ]] || { echo "FATAL: ${pipeline} not found"; exit 1; }

# A well-formed 40-character SHA, and a different one for the mismatch case.
SHA_A="1111111111111111111111111111111111111111"
SHA_B="2222222222222222222222222222222222222222"

# Fields, pipe-separated:
#   name | expected_rc | expected_message_fragment
#   | BUILD_REASON | BUILD_SOURCEBRANCH | BUILD_SOURCEVERSION
#   | STG_SOURCE_BRANCH | STG_SOURCE_COMMIT | STG_RUN_ID
#
# The sentinel <unset> omits a variable entirely rather than setting it empty.
CASES=(
  "happy path|0|Promotion gate passed: promoting STG run 1234 at ${SHA_A}.|ResourceTrigger|refs/heads/main|${SHA_A}|refs/heads/main|${SHA_A}|1234"
  "rejects non-ResourceTrigger run|1|PROD runs only via STG CD completion (Build.Reason='Manual').|Manual|refs/heads/main|${SHA_A}|refs/heads/main|${SHA_A}|1234"
  "rejects PROD off refs/heads/main|1|PROD must build refs/heads/main (got 'refs/heads/feature/x').|ResourceTrigger|refs/heads/feature/x|${SHA_A}|refs/heads/main|${SHA_A}|1234"
  "rejects upstream STG off main|1|Triggering STG run must originate from refs/heads/main (got 'refs/heads/release').|ResourceTrigger|refs/heads/main|${SHA_A}|refs/heads/release|${SHA_A}|1234"
  "rejects empty upstream run ID|1|Triggering STG run ID is empty.|ResourceTrigger|refs/heads/main|${SHA_A}|refs/heads/main|${SHA_A}|"
  "rejects unresolved upstream run ID|1|Triggering STG run ID is unresolved or invalid (got '\$(resources.pipeline.stg.runID)').|ResourceTrigger|refs/heads/main|${SHA_A}|refs/heads/main|${SHA_A}|\$(resources.pipeline.stg.runID)"
  "rejects same-SHA mismatch|1|Commit mismatch: PROD '${SHA_A}' != STG '${SHA_B}'.|ResourceTrigger|refs/heads/main|${SHA_A}|refs/heads/main|${SHA_B}|1234"
  # Not one of the seven pipeline cases: proves the gate fails closed when the
  # variables are absent altogether, rather than aborting on `set -u`.
  "fails closed when inputs unset|1|PROD runs only via STG CD completion (Build.Reason='').|<unset>|<unset>|<unset>|<unset>|<unset>|<unset>"
)

passed=0
failed=0

run_case() {
  local name="$1" want_rc="$2" want_msg="$3"
  local reason="$4" branch="$5" sha="$6" up_branch="$7" up_sha="$8" up_run="$9"

  local -a env_args=()
  [[ "${reason}"    == "<unset>" ]] || env_args+=("BUILD_REASON=${reason}")
  [[ "${branch}"    == "<unset>" ]] || env_args+=("BUILD_SOURCEBRANCH=${branch}")
  [[ "${sha}"       == "<unset>" ]] || env_args+=("BUILD_SOURCEVERSION=${sha}")
  [[ "${up_branch}" == "<unset>" ]] || env_args+=("STG_SOURCE_BRANCH=${up_branch}")
  [[ "${up_sha}"    == "<unset>" ]] || env_args+=("STG_SOURCE_COMMIT=${up_sha}")
  [[ "${up_run}"    == "<unset>" ]] || env_args+=("STG_RUN_ID=${up_run}")

  local output rc
  # ${arr[@]+...} guards the empty-array case: under `set -u`, bash 3.2 (the
  # macOS system bash) treats a bare "${env_args[@]}" expansion of an empty
  # array as an unbound variable.
  output="$(env -i "PATH=${PATH}" ${env_args[@]+"${env_args[@]}"} "${gate}" 2>&1)"
  rc=$?

  if [[ "${rc}" -ne "${want_rc}" ]]; then
    printf 'FAIL  %s\n        expected exit %s, got %s\n        output: %s\n' \
      "${name}" "${want_rc}" "${rc}" "$(echo "${output}" | tail -1)"
    failed=$((failed + 1))
    return
  fi

  if ! grep -qF -- "${want_msg}" <<<"${output}"; then
    printf 'FAIL  %s\n        exit %s correct, but message did not match\n        expected: %s\n        actual:   %s\n' \
      "${name}" "${rc}" "${want_msg}" "$(echo "${output}" | tail -1)"
    failed=$((failed + 1))
    return
  fi

  printf 'ok    %s (exit %s)\n' "${name}" "${rc}"
  passed=$((passed + 1))
}

echo "== scripts/ci/verify-promotion.sh — gate behaviour"
for case_row in "${CASES[@]}"; do
  IFS='|' read -r name want_rc want_msg reason branch sha up_branch up_sha up_run <<<"${case_row}"
  run_case "${name}" "${want_rc}" "${want_msg}" \
    "${reason}" "${branch}" "${sha}" "${up_branch}" "${up_sha}" "${up_run}"
done

echo
echo "== azure-pipelines/databricks-cd-prod.yml — gate wiring"

assert_pipeline() {
  local name="$1" pattern="$2"
  if grep -qF -- "${pattern}" "${pipeline}"; then
    printf 'ok    %s\n' "${name}"
    passed=$((passed + 1))
  else
    printf 'FAIL  %s\n        not found in %s: %s\n' \
      "${name}" "${pipeline##*/}" "${pattern}"
    failed=$((failed + 1))
  fi
}

assert_pipeline "invokes the extracted script"      "bash: ./scripts/ci/verify-promotion.sh"
assert_pipeline "BUILD_REASON <- Build.Reason"       "BUILD_REASON: \$(Build.Reason)"
assert_pipeline "BUILD_SOURCEBRANCH <- SourceBranch" "BUILD_SOURCEBRANCH: \$(Build.SourceBranch)"
assert_pipeline "BUILD_SOURCEVERSION <- SourceVersion" "BUILD_SOURCEVERSION: \$(Build.SourceVersion)"
assert_pipeline "STG_SOURCE_BRANCH <- stg.sourceBranch" "STG_SOURCE_BRANCH: \$(resources.pipeline.stg.sourceBranch)"
assert_pipeline "STG_SOURCE_COMMIT <- stg.sourceCommit" "STG_SOURCE_COMMIT: \$(resources.pipeline.stg.sourceCommit)"
assert_pipeline "STG_RUN_ID <- stg.runID"            "STG_RUN_ID: \$(resources.pipeline.stg.runID)"
assert_pipeline "gate stage checks out the repo"     "- checkout: self"
assert_pipeline "PROD approval Environment retained" "environment: aiplatform-prod"

echo
echo "passed=${passed} failed=${failed}"
[[ "${failed}" -eq 0 ]]
