#!/usr/bin/env bash
#
# Local tests for products/ml-lifecycle-demo/scripts/model-promotion.sh.
#
# The helper is a fail-closed wrapper over Databricks CLI calls, so its whole
# contract can be proven locally by MOCKING the CLI: a stub `databricks` on PATH
# replays canned JSON and records the arguments it was called with. No Azure, no
# Databricks, no workspace, no credential, and no model is promoted.
#
# Each case asserts BOTH the exit status and the message — a check that failed
# for the wrong reason is not a passing check. The mutation case additionally
# asserts the exact CLI argv, because "did it call set-alias with the right
# version" is the one thing a message cannot prove.
#
# Usage:  ./tests/shell/test-model-promotion.sh
# Exit:   0 all tests passed, 1 one or more failed.
#
set -uo pipefail

product_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
helper="${product_root}/scripts/model-promotion.sh"

[[ -x "${helper}" ]] || { echo "FATAL: ${helper} missing or not executable"; exit 1; }

MODEL="prod.ml_lifecycle_demo.linear_regression_model"
SHA_A="1111111111111111111111111111111111111111"
SHA_B="2222222222222222222222222222222222222222"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

mockbin="${workdir}/bin"
mkdir -p "${mockbin}"

# The stub CLI. Behaviour is driven entirely by files the individual test writes
# into ${workdir}, so each case controls exactly what the "workspace" returns.
cat > "${mockbin}/databricks" <<'MOCK'
#!/usr/bin/env bash
set -uo pipefail

echo "$*" >> "${MOCK_CALLS}"

case "$1 $2" in
  "model-versions get-by-alias")
    [[ -s "${MOCK_DIR}/alias.json" ]] || { echo "alias not found" >&2; exit 1; }
    cat "${MOCK_DIR}/alias.json"
    ;;
  "model-versions get")
    [[ -s "${MOCK_DIR}/version.json" ]] || { echo "version not found" >&2; exit 1; }
    cat "${MOCK_DIR}/version.json"
    ;;
  "experiments get-run")
    [[ -s "${MOCK_DIR}/run.json" ]] || { echo "run not found" >&2; exit 1; }
    cat "${MOCK_DIR}/run.json"
    ;;
  "registered-models set-alias")
    if [[ -f "${MOCK_DIR}/set-alias-fails" ]]; then
      echo "PERMISSION_DENIED" >&2
      exit 1
    fi
    echo "{}"
    ;;
  *)
    echo "unexpected CLI call: $*" >&2
    exit 1
    ;;
esac
MOCK
chmod +x "${mockbin}/databricks"

passed=0
failed=0

# version_json <version> <catalog> [tags-json]
version_json() {
  local version="$1" catalog="$2" tags="${3-[]}"
  cat <<JSON
{
  "catalog_name": "${catalog}",
  "schema_name": "ml_lifecycle_demo",
  "model_name": "linear_regression_model",
  "version": ${version},
  "status": "READY",
  "run_id": "run-abc",
  "source": "dbfs:/databricks/mlflow-tracking/1/run-abc/artifacts/model",
  "storage_location": "abfss://uc@example.dfs.core.windows.net/models/1",
  "tags": ${tags}
}
JSON
}

reset_mock() {
  rm -f "${workdir}"/*.json "${workdir}/set-alias-fails" "${workdir}/calls.txt"
  : > "${workdir}/calls.txt"
}

# check <name> <expected_rc> <expected_fragment> -- <helper args...>
check() {
  local name="$1" want_rc="$2" want_msg="$3"
  shift 4  # name, rc, msg, and the literal --

  local output rc
  output="$(
    PATH="${mockbin}:${PATH}" \
    MOCK_DIR="${workdir}" \
    MOCK_CALLS="${workdir}/calls.txt" \
    "${helper}" "$@" 2>&1
  )"
  rc=$?

  if [[ "${rc}" -ne "${want_rc}" ]]; then
    echo "FAIL: ${name}"
    echo "      expected rc ${want_rc}, got ${rc}"
    echo "      output: ${output}"
    failed=$((failed + 1))
    return
  fi

  if [[ -n "${want_msg}" && "${output}" != *"${want_msg}"* ]]; then
    echo "FAIL: ${name}"
    echo "      expected message containing: ${want_msg}"
    echo "      actual output:               ${output}"
    failed=$((failed + 1))
    return
  fi

  echo "PASS: ${name}"
  passed=$((passed + 1))
}

TAGS_GOOD='[{"key":"environment","value":"prod"},{"key":"git_sha","value":"'"${SHA_A}"'"},{"key":"release_status","value":"candidate"}]'
TAGS_WRONG_ENV='[{"key":"environment","value":"stg"},{"key":"git_sha","value":"'"${SHA_A}"'"}]'
TAGS_WRONG_SHA='[{"key":"environment","value":"prod"},{"key":"git_sha","value":"'"${SHA_B}"'"}]'

# --- resolve-alias ----------------------------------------------------------

reset_mock
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/alias.json"
check "resolve-alias prints the bare version" 0 "7" -- resolve-alias "${MODEL}" Candidate

# The captured value must be EXACTLY the number: a pipeline interpolates it into
# a model URI and into a set-alias call.
reset_mock
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/alias.json"
captured="$(
  PATH="${mockbin}:${PATH}" MOCK_DIR="${workdir}" MOCK_CALLS="${workdir}/calls.txt" \
  "${helper}" resolve-alias "${MODEL}" Candidate 2>/dev/null
)"
if [[ "${captured}" == "7" ]]; then
  echo "PASS: resolve-alias stdout carries only the version"
  passed=$((passed + 1))
else
  echo "FAIL: resolve-alias stdout carries only the version (got '${captured}')"
  failed=$((failed + 1))
fi

reset_mock
check "resolve-alias fails closed when the alias cannot be resolved" 1 \
  "could not resolve alias Candidate" \
  -- resolve-alias "${MODEL}" Candidate

reset_mock
echo '{"version": null}' > "${workdir}/alias.json"
check "resolve-alias fails closed on a null version" 1 "is empty" \
  -- resolve-alias "${MODEL}" Candidate

reset_mock
echo '{"version": "not-a-number"}' > "${workdir}/alias.json"
check "resolve-alias fails closed on a non-numeric version" 1 "not a positive integer" \
  -- resolve-alias "${MODEL}" Candidate

# --- verify-version ---------------------------------------------------------

reset_mock
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/version.json"
check "verify-version accepts a matching version" 0 "version 7 verified" \
  -- verify-version "${MODEL}" 7 --environment prod --git-sha "${SHA_A}" --require-ready

reset_mock
version_json 7 stg "${TAGS_WRONG_ENV}" > "${workdir}/version.json"
check "verify-version rejects the wrong catalog" 1 "lives in catalog 'stg', expected 'prod'" \
  -- verify-version "${MODEL}" 7 --environment prod

reset_mock
version_json 7 prod "${TAGS_WRONG_SHA}" > "${workdir}/version.json"
check "verify-version rejects the wrong commit" 1 "trained from commit ${SHA_B}" \
  -- verify-version "${MODEL}" 7 --environment prod --git-sha "${SHA_A}"

reset_mock
version_json 7 prod '[]' > "${workdir}/version.json"
check "verify-version fails closed when a demanded git_sha is absent" 1 "records no git_sha" \
  -- verify-version "${MODEL}" 7 --environment prod --git-sha "${SHA_A}"

# The run-tag fallback: UC returns no model-version tags, but the training run
# does. A readable provenance record must not read as absent.
reset_mock
version_json 7 prod '[]' > "${workdir}/version.json"
cat > "${workdir}/run.json" <<JSON
{"run": {"data": {"tags": [{"key": "git_sha", "value": "${SHA_A}"}]}}}
JSON
check "verify-version falls back to the training run's git_sha" 0 "version 7 verified" \
  -- verify-version "${MODEL}" 7 --environment prod --git-sha "${SHA_A}"

reset_mock
version_json 7 prod '[]' > "${workdir}/version.json"
cat > "${workdir}/run.json" <<JSON
{"run": {"data": {"tags": [{"key": "git_sha", "value": "${SHA_B}"}]}}}
JSON
check "verify-version rejects a mismatched fallback git_sha" 1 "trained from commit ${SHA_B}" \
  -- verify-version "${MODEL}" 7 --environment prod --git-sha "${SHA_A}"

reset_mock
version_json 7 prod "${TAGS_GOOD}" | sed 's/"READY"/"PENDING_REGISTRATION"/' > "${workdir}/version.json"
check "verify-version rejects a non-READY version" 1 "status is 'PENDING_REGISTRATION'" \
  -- verify-version "${MODEL}" 7 --environment prod --require-ready

# A non-READY version passes when READY was not demanded, so --require-ready is
# proven to be the thing doing the work above.
reset_mock
version_json 7 prod "${TAGS_GOOD}" | sed 's/"READY"/"PENDING_REGISTRATION"/' > "${workdir}/version.json"
check "verify-version ignores status unless --require-ready" 0 "version 7 verified" \
  -- verify-version "${MODEL}" 7 --environment prod

reset_mock
version_json 9 prod "${TAGS_GOOD}" > "${workdir}/version.json"
check "verify-version rejects a version the API did not return" 1 "asked for version 7 but got 9" \
  -- verify-version "${MODEL}" 7 --environment prod

reset_mock
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/version.json"
check "verify-version rejects an unresolved pipeline macro" 1 "not a positive integer" \
  -- verify-version "${MODEL}" '$(candidateVersion)' --environment prod

reset_mock
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/version.json"
check "verify-version requires --environment" 1 "--environment is required" \
  -- verify-version "${MODEL}" 7

reset_mock
echo 'not json at all' > "${workdir}/version.json"
check "verify-version fails closed on malformed JSON" 1 "malformed JSON" \
  -- verify-version "${MODEL}" 7 --environment prod

# --- promote ----------------------------------------------------------------

reset_mock
check "promote moves Champion to an exact version" 0 "Champion -> ${MODEL} version 7" \
  -- promote "${MODEL}" 7

# The argv assertion: the message alone cannot prove WHICH version was set.
if grep -qx "registered-models set-alias ${MODEL} Champion 7" "${workdir}/calls.txt"; then
  echo "PASS: promote calls set-alias with the exact version"
  passed=$((passed + 1))
else
  echo "FAIL: promote calls set-alias with the exact version"
  echo "      calls: $(cat "${workdir}/calls.txt")"
  failed=$((failed + 1))
fi

reset_mock
check "promote refuses a non-numeric version" 1 "not a positive integer" \
  -- promote "${MODEL}" Candidate

# Nothing may be mutated when the input is rejected.
if [[ ! -s "${workdir}/calls.txt" ]]; then
  echo "PASS: promote makes no CLI call when the version is rejected"
  passed=$((passed + 1))
else
  echo "FAIL: promote makes no CLI call when the version is rejected"
  echo "      calls: $(cat "${workdir}/calls.txt")"
  failed=$((failed + 1))
fi

reset_mock
touch "${workdir}/set-alias-fails"
check "promote surfaces a set-alias failure" 1 "could not set Champion" \
  -- promote "${MODEL}" 7

# --- verify-alias -----------------------------------------------------------

reset_mock
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/alias.json"
check "verify-alias accepts the expected version" 0 "Champion -> ${MODEL} version 7" \
  -- verify-alias "${MODEL}" Champion 7

reset_mock
version_json 8 prod "${TAGS_GOOD}" > "${workdir}/alias.json"
check "verify-alias rejects a drifted alias" 1 "Champion points to version 8, expected 7" \
  -- verify-alias "${MODEL}" Champion 7

# --- write-manifest / verify-manifest ---------------------------------------
#
# The release manifest replaces a stage output variable as the carrier of the
# approved version across the PROD approval boundary, so its validation is the
# thing standing between an approval and the wrong artefact being released.

manifest="${workdir}/model-release-manifest.json"

reset_mock
rm -f "${manifest}"
version_json 7 prod "${TAGS_GOOD}" > "${workdir}/version.json"
check "write-manifest writes a verified manifest" 0 "Wrote release manifest" \
  -- write-manifest "${MODEL}" 7 --environment prod --git-sha "${SHA_A}" --output "${manifest}"

if [[ -f "${manifest}" ]]; then
  missing=""
  for field in product full_model_name catalog schema candidate_version git_sha run_id source status; do
    jq -e --arg f "${field}" 'has($f)' "${manifest}" >/dev/null 2>&1 || missing="${missing} ${field}"
  done
  if [[ -z "${missing}" ]]; then
    echo "PASS: manifest contains every required field"
    passed=$((passed + 1))
  else
    echo "FAIL: manifest is missing fields:${missing}"
    failed=$((failed + 1))
  fi
else
  echo "FAIL: manifest was not written"
  failed=$((failed + 1))
fi

# candidate_version must be a STRING in the manifest, so a consumer that reads
# it with `jq -r` gets "7" rather than a number that could be reformatted.
if [[ "$(jq -r '.candidate_version | type' "${manifest}" 2>/dev/null)" == "string" ]]; then
  echo "PASS: manifest candidate_version is a string"
  passed=$((passed + 1))
else
  echo "FAIL: manifest candidate_version is not a string"
  failed=$((failed + 1))
fi

# A manifest for a version that fails verification must never be written.
reset_mock
rm -f "${manifest}"
version_json 7 stg "${TAGS_WRONG_ENV}" > "${workdir}/version.json"
check "write-manifest refuses to record an unverified version" 1 "lives in catalog 'stg'" \
  -- write-manifest "${MODEL}" 7 --environment prod --git-sha "${SHA_A}" --output "${manifest}"

if [[ ! -f "${manifest}" ]]; then
  echo "PASS: no manifest is written when verification fails"
  passed=$((passed + 1))
else
  echo "FAIL: a manifest was written despite failed verification"
  failed=$((failed + 1))
fi

# --- verify-manifest --------------------------------------------------------

write_manifest_fixture() {
  local version="${1-7}" catalog="${2-prod}" sha="${3-${SHA_A}}" status="${4-READY}"
  local product="${5-ml-lifecycle-demo}" model="${6-${MODEL}}"
  cat > "${manifest}" <<JSON
{
  "product": "${product}",
  "full_model_name": "${model}",
  "catalog": "${catalog}",
  "schema": "ml_lifecycle_demo",
  "candidate_version": "${version}",
  "git_sha": "${sha}",
  "run_id": "run-abc",
  "source": "dbfs:/x/artifacts/model",
  "status": "${status}"
}
JSON
}

reset_mock
write_manifest_fixture
captured="$(
  PATH="${mockbin}:${PATH}" MOCK_DIR="${workdir}" MOCK_CALLS="${workdir}/calls.txt" \
  "${helper}" verify-manifest "${manifest}" \
    --model "${MODEL}" --environment prod --git-sha "${SHA_A}" 2>/dev/null
)"
if [[ "${captured}" == "7" ]]; then
  echo "PASS: verify-manifest stdout carries only the version"
  passed=$((passed + 1))
else
  echo "FAIL: verify-manifest stdout carries only the version (got '${captured}')"
  failed=$((failed + 1))
fi

# A genuine, internally consistent STG manifest handed to the PROD release
# stage. This is the realistic confusion the catalog check exists to stop, so
# the fixture is self-consistent and --model matches it: the ONLY thing wrong is
# the environment.
reset_mock
write_manifest_fixture 7 stg "${SHA_A}" READY ml-lifecycle-demo \
  stg.ml_lifecycle_demo.linear_regression_model
check "verify-manifest rejects a valid manifest from the wrong environment" 1 \
  "for catalog 'stg', expected 'prod'" \
  -- verify-manifest "${manifest}" \
     --model stg.ml_lifecycle_demo.linear_regression_model \
     --environment prod --git-sha "${SHA_A}"

reset_mock
write_manifest_fixture 7 prod "${SHA_B}"
check "verify-manifest rejects a manifest from another commit" 1 "for commit ${SHA_B}" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

reset_mock
write_manifest_fixture 7 prod "${SHA_A}" PENDING_REGISTRATION
check "verify-manifest rejects a non-READY status" 1 "status 'PENDING_REGISTRATION', expected READY" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

reset_mock
write_manifest_fixture "" prod
check "verify-manifest rejects an empty version" 1 "missing 'candidate_version'" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

reset_mock
write_manifest_fixture 'latest' prod
check "verify-manifest rejects a non-numeric version" 1 "not a positive integer" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

reset_mock
write_manifest_fixture 7 prod "${SHA_A}" READY hello-databricks
check "verify-manifest rejects another product's manifest" 1 "for product 'hello-databricks'" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

reset_mock
write_manifest_fixture 7 prod "${SHA_A}" READY ml-lifecycle-demo stg.ml_lifecycle_demo.linear_regression_model
check "verify-manifest rejects a manifest naming another model" 1 "names model 'stg." \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

# A hand-edited manifest claiming the PROD model while pointing at another
# catalog must not pass: the name and the catalog field have to agree.
reset_mock
cat > "${manifest}" <<JSON
{
  "product": "ml-lifecycle-demo",
  "full_model_name": "${MODEL}",
  "catalog": "dev",
  "schema": "ml_lifecycle_demo",
  "candidate_version": "7",
  "git_sha": "${SHA_A}",
  "run_id": "run-abc",
  "source": "dbfs:/x",
  "status": "READY"
}
JSON
check "verify-manifest rejects a self-inconsistent manifest" 1 "does not match catalog 'dev'" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment dev --git-sha "${SHA_A}"

reset_mock
rm -f "${manifest}"
check "verify-manifest fails closed when the artifact is absent" 1 "release manifest not found" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

reset_mock
echo 'not json' > "${manifest}"
check "verify-manifest fails closed on malformed JSON" 1 "not valid JSON" \
  -- verify-manifest "${manifest}" --model "${MODEL}" --environment prod --git-sha "${SHA_A}"

# verify-manifest must be a pure file check: it must NOT reach for the alias.
reset_mock
write_manifest_fixture
PATH="${mockbin}:${PATH}" MOCK_DIR="${workdir}" MOCK_CALLS="${workdir}/calls.txt" \
  "${helper}" verify-manifest "${manifest}" \
    --model "${MODEL}" --environment prod --git-sha "${SHA_A}" >/dev/null 2>&1
if ! grep -q "get-by-alias" "${workdir}/calls.txt"; then
  echo "PASS: verify-manifest never re-resolves the Candidate alias"
  passed=$((passed + 1))
else
  echo "FAIL: verify-manifest re-resolved the Candidate alias"
  echo "      calls: $(cat "${workdir}/calls.txt")"
  failed=$((failed + 1))
fi

# --- CD pipeline wiring: helper steps must not run inside the Bundle root ----
#
# A RUNTIME AUTHENTICATION DEFECT, caught statically.
#
# The Databricks CLI auto-discovers databricks.yml when its working directory is
# inside a Bundle, and then resolves the host from that Bundle's DEFAULT target —
# which is dev. A helper step running with workingDirectory=$(productDir)
# therefore combined the STG or PROD service-principal client ID with the DEV
# workspace host, and the failure surfaced as "alias not found" rather than as
# anything resembling an auth problem.
#
# Reproduced directly:
#   cd <repo>                 && DATABRICKS_HOST=<stg> databricks auth describe
#       -> Host: <stg>, workspace_id 1000000000000003
#   cd <repo>/products/ml-...  && DATABRICKS_HOST=<stg> databricks auth describe
#       -> resolves the aiplatform-dev profile instead
#
# Control-plane calls (the helper, and `databricks tables get`) must therefore
# run from $(Build.SourcesDirectory), where DATABRICKS_HOST is authoritative.
# `databricks bundle` calls must keep running in $(productDir), because those
# genuinely need the Bundle. These assertions pin both halves: a future edit that
# moves a helper step back inside the Bundle root fails here, in CI, instead of
# silently promoting against the wrong workspace.

pipeline_dir="$(cd "${product_root}/../../azure-pipelines" && pwd)"

assert_pipeline_wiring() {
  local file="$1" path="${pipeline_dir}/$1"

  [[ -f "${path}" ]] || {
    echo "FAIL: ${file} not found"
    failed=$((failed + 1))
    return
  }

  # 1. The helper is always invoked by its repository-relative path.
  if grep -q '\./scripts/model-promotion\.sh' "${path}"; then
    echo "FAIL: ${file} invokes the helper by a Bundle-relative path"
    echo "      use ./products/ml-lifecycle-demo/scripts/model-promotion.sh"
    failed=$((failed + 1))
  else
    echo "PASS: ${file} invokes the helper by its repository-relative path"
    passed=$((passed + 1))
  fi

  # 2. The helper is actually invoked at all — guards against a rename that
  #    would make check 1 vacuously true.
  if grep -q './products/ml-lifecycle-demo/scripts/model-promotion\.sh' "${path}"; then
    echo "PASS: ${file} invokes the promotion helper"
    passed=$((passed + 1))
  else
    echo "FAIL: ${file} invokes the promotion helper (no invocation found)"
    failed=$((failed + 1))
  fi

  # 3. No step that calls the helper may run from the Bundle root. Awk walks
  #    step blocks so the check is per-step, not per-file: the same file
  #    legitimately contains productDir steps for `databricks bundle`.
  local offenders
  offenders="$(
    awk '
      /^[[:space:]]*- (bash|task):/ {
        if (block != "" && block ~ /model-promotion\.sh/ && block ~ /workingDirectory: \$\(productDir\)/) print name
        block = ""; name = "(unnamed step)"
      }
      /displayName:/ { line = $0; sub(/^[[:space:]]*displayName:[[:space:]]*/, "", line); name = line }
      { block = block "\n" $0 }
      END {
        if (block != "" && block ~ /model-promotion\.sh/ && block ~ /workingDirectory: \$\(productDir\)/) print name
      }
    ' "${path}"
  )"

  if [[ -n "${offenders}" ]]; then
    echo "FAIL: ${file} runs promotion-helper steps from \$(productDir):"
    echo "${offenders}" | sed 's/^/        /'
    failed=$((failed + 1))
  else
    echo "PASS: ${file} runs no promotion-helper step from \$(productDir)"
    passed=$((passed + 1))
  fi

  # 4. Bundle commands must NOT have been moved out of the Bundle root by an
  #    over-eager fix. The two halves of this defect are symmetrical.
  local stray
  stray="$(
    awk '
      /^[[:space:]]*- (bash|task):/ {
        if (block != "" && block ~ /databricks bundle/ && block !~ /workingDirectory: \$\(productDir\)/) print name
        block = ""; name = "(unnamed step)"
      }
      /displayName:/ { line = $0; sub(/^[[:space:]]*displayName:[[:space:]]*/, "", line); name = line }
      { block = block "\n" $0 }
      END {
        if (block != "" && block ~ /databricks bundle/ && block !~ /workingDirectory: \$\(productDir\)/) print name
      }
    ' "${path}"
  )"

  if [[ -n "${stray}" ]]; then
    echo "FAIL: ${file} runs \`databricks bundle\` outside \$(productDir):"
    echo "${stray}" | sed 's/^/        /'
    failed=$((failed + 1))
  else
    echo "PASS: ${file} keeps every \`databricks bundle\` step in \$(productDir)"
    passed=$((passed + 1))
  fi
}

assert_pipeline_wiring ml-lifecycle-demo-dev-cd.yml
assert_pipeline_wiring ml-lifecycle-demo-stg-cd.yml
assert_pipeline_wiring ml-lifecycle-demo-prod-cd.yml

# --- usage ------------------------------------------------------------------

reset_mock
check "unknown subcommand fails closed" 1 "unknown subcommand 'promote-everything'" \
  -- promote-everything "${MODEL}"

reset_mock
check "no subcommand fails closed" 1 "usage:" --

echo
echo "passed: ${passed}  failed: ${failed}"
[[ "${failed}" -eq 0 ]]
