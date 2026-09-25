#!/usr/bin/env bash
#
# Governed same-SHA STG -> PROD promotion gate.
#
# Extracted verbatim from the inline gate in
# azure-pipelines/databricks-cd-prod.yml so the deterministic part of the
# promotion contract can be executed and tested locally instead of only inside a
# pipeline run.
#
# SCOPE / LIMITATIONS (read before trusting this):
#   This script validates SUPPLIED VALUES ONLY. It performs no Azure, Azure
#   DevOps or Databricks API call, reads no state, deploys nothing, and infers
#   nothing about the run it is describing — it cannot tell whether the values
#   it was handed are truthful. Its guarantee is narrow and worth stating
#   plainly: given a set of run-identity values, it decides whether that
#   combination constitutes a legitimate STG -> PROD promotion. The authority
#   that makes those values trustworthy is Azure Pipelines' pipeline-resource
#   resolution, and the authoritative human gate remains the manual approval on
#   the aiplatform-prod Environment. Neither is replaced by this file.
#
# Inputs (environment variables). An unset variable is treated as empty and
# fails closed, exactly as an unresolved pipeline macro would:
#
#   BUILD_REASON          this run's trigger reason (must be ResourceTrigger)
#   BUILD_SOURCEBRANCH    this run's branch (must be refs/heads/main)
#   BUILD_SOURCEVERSION   this run's commit SHA
#   STG_SOURCE_BRANCH     triggering STG run's branch (must be refs/heads/main)
#   STG_SOURCE_COMMIT     triggering STG run's commit SHA (must equal ours)
#   STG_RUN_ID            triggering STG run's ID (must be non-empty, numeric)
#
# Exit status:
#   0  promotion gate passed
#   1  promotion gate failed (message on stdout, prefixed PROMOTION GATE FAILED)
#
set -euo pipefail

# Default every input to empty rather than letting `set -u` abort on an unset
# variable. This keeps the failure path inside the explicit checks below, so a
# missing value produces the same actionable gate message locally as an
# unresolved macro does in the pipeline.
BUILD_REASON="${BUILD_REASON-}"
BUILD_SOURCEBRANCH="${BUILD_SOURCEBRANCH-}"
BUILD_SOURCEVERSION="${BUILD_SOURCEVERSION-}"
STG_SOURCE_BRANCH="${STG_SOURCE_BRANCH-}"
STG_SOURCE_COMMIT="${STG_SOURCE_COMMIT-}"
STG_RUN_ID="${STG_RUN_ID-}"

echo "Build.Reason:                        ${BUILD_REASON}"
echo "Build.SourceBranch:                  ${BUILD_SOURCEBRANCH}"
echo "Build.SourceVersion:                 ${BUILD_SOURCEVERSION}"
echo "resources.pipeline.stg.sourceBranch: ${STG_SOURCE_BRANCH}"
echo "resources.pipeline.stg.sourceCommit: ${STG_SOURCE_COMMIT}"
echo "resources.pipeline.stg.runID:        ${STG_RUN_ID}"

fail() {
  echo "PROMOTION GATE FAILED: $1"
  exit 1
}

[[ "${BUILD_REASON}" == "ResourceTrigger" ]] ||
  fail "PROD runs only via STG CD completion (Build.Reason='${BUILD_REASON}')."

[[ "${BUILD_SOURCEBRANCH}" == "refs/heads/main" ]] ||
  fail "PROD must build refs/heads/main (got '${BUILD_SOURCEBRANCH}')."

[[ "${STG_SOURCE_BRANCH}" == "refs/heads/main" ]] ||
  fail "Triggering STG run must originate from refs/heads/main (got '${STG_SOURCE_BRANCH}')."

[[ -n "${STG_RUN_ID}" ]] ||
  fail "Triggering STG run ID is empty."

[[ "${STG_RUN_ID}" =~ ^[0-9]+$ ]] ||
  fail "Triggering STG run ID is unresolved or invalid (got '${STG_RUN_ID}')."

[[ "${BUILD_SOURCEVERSION}" == "${STG_SOURCE_COMMIT}" ]] ||
  fail "Commit mismatch: PROD '${BUILD_SOURCEVERSION}' != STG '${STG_SOURCE_COMMIT}'."

echo "Promotion gate passed: promoting STG run ${STG_RUN_ID} at ${BUILD_SOURCEVERSION}."
