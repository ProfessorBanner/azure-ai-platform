#!/usr/bin/env bash
#
# Candidate -> Champion promotion helper for ml-lifecycle-demo.
#
# ONE implementation of the fragile parts of the promotion contract — resolving
# a mutable alias to an exact version, proving that version's provenance, moving
# Champion, and confirming the move — so the DEV, STG and PROD pipelines call it
# instead of each carrying its own copy of the same jq. Three copies of this
# logic would drift, and the copy that drifted would be the one guarding PROD.
#
# SCOPE / LIMITATIONS (read before trusting this):
#   Every subcommand is a thin, fail-closed wrapper over documented Databricks
#   CLI calls. It creates nothing, trains nothing and deletes nothing; the only
#   mutation it can perform is `registered-models set-alias`, in `promote`.
#   Authentication is entirely the caller's (the pipeline's OIDC environment) —
#   this script never reads a credential, never writes one, and never changes
#   auth configuration.
#
#   It deliberately CANNOT write model-version tags. Tags are immutable
#   provenance, written exactly once at registration by the MLflow client on the
#   cluster, in train_register.py and nowhere else. (The Databricks CLI could not
#   do it in any case: `model-versions update` documents "Currently only the
#   comment of the model version can be updated", and `entity-tag-assignments`
#   covers catalogs, schemas, tables, columns and volumes — not models.)
#
# Usage:
#   model-promotion.sh resolve-alias      FULL_NAME ALIAS
#   model-promotion.sh verify-version     FULL_NAME VERSION --environment ENV
#                                         [--git-sha SHA] [--require-ready]
#   model-promotion.sh promote            FULL_NAME VERSION
#   model-promotion.sh verify-alias       FULL_NAME ALIAS VERSION
#   model-promotion.sh describe-version   FULL_NAME VERSION
#   model-promotion.sh write-manifest     FULL_NAME VERSION --environment ENV
#                                         --git-sha SHA --output PATH
#   model-promotion.sh verify-manifest    PATH --model FULL_NAME --environment ENV
#                                         --git-sha SHA
#
# resolve-alias prints ONLY the version number on stdout, so a pipeline can
# capture it with $(...). Every other subcommand's diagnostics go to stdout for
# the build log; failures print a PROMOTION line and exit 1.
#
# Exit status:
#   0  the assertion held
#   1  it did not (message on stderr, prefixed PROMOTION FAILED)
#
set -uo pipefail

# Failures go to STDERR, deliberately. Several subcommands are called inside
# $(...) by other subcommands and by the pipelines; a message written to stdout
# there would be captured into the caller's variable instead of reaching the
# build log, turning a clear failure into a silently corrupted value.
fail() {
  echo "PROMOTION FAILED: $1" >&2
  exit 1
}

# Must match ml_logic.PRODUCT_NAME. tests/unit/test_promotion.py asserts the two
# agree, so a rename in Python cannot silently desynchronise this file.
readonly PRODUCT_NAME="ml-lifecycle-demo"

# Databricks CLI JSON, per the documented response fields:
#   model-versions get      -> catalog_name schema_name model_name version
#                              status run_id source storage_location [tags]
#   model-versions get-by-alias -> the same shape for the aliased version
#   experiments get-run     -> .run.data.tags[] as {key, value}
#
# Tag arrays are read defensively: `tags` is documented as present-when-set, and
# a version with no tags must read as "no tags" rather than as a jq error.
readonly TAG_LOOKUP='
  def tagval($k):
    ( .tags // [] )
    | map(select((.key // .tag_key) == $k))
    | if length == 0 then empty else (.[0].value // .[0].tag_value // empty) end;
'

require_args() {
  local want="$1" got="$2" usage="$3"
  [[ "${got}" -ge "${want}" ]] || fail "usage: ${usage}"
}

# A version number must be a bare positive integer. Azure Pipelines hands over
# an unresolved macro as a literal string when a variable is missing, and jq
# hands over "null" for an absent field; both must fail here rather than become
# part of a model URI.
assert_numeric_version() {
  local value="$1" label="$2"

  [[ -n "${value}" ]] || fail "${label} is empty"
  [[ "${value}" =~ ^[0-9]+$ ]] || fail "${label} is not a positive integer (got '${value}')"
  [[ "${value}" != "0" ]] || fail "${label} is not a positive integer (got '0')"
}

model_version_json() {
  local full_name="$1" version="$2" payload

  payload="$(databricks model-versions get "${full_name}" "${version}" -o json 2>&1)" ||
    fail "could not read ${full_name} version ${version}: ${payload}"

  echo "${payload}" | jq -e . >/dev/null 2>&1 ||
    fail "malformed JSON from model-versions get ${full_name} ${version}"

  echo "${payload}"
}

# --- resolve-alias ----------------------------------------------------------
#
# Resolve a MUTABLE alias to an exact version, ONCE. Callers must then work from
# the number: between this call and the next, a concurrent run can move the
# alias, and a pipeline that re-resolved would promote something nobody
# evaluated.
cmd_resolve_alias() {
  require_args 2 $# "model-promotion.sh resolve-alias FULL_NAME ALIAS"
  local full_name="$1" alias_name="$2" payload version

  # "could not resolve", not "not found". This call fails for reasons that have
  # nothing to do with the alias: an expired token, the wrong workspace host, a
  # principal without EXECUTE on the model. Reporting all of those as a missing
  # alias sends the reader looking in the registry for something that was never
  # the problem — which is exactly what happened when the helper inherited the
  # DEV host from an auto-discovered databricks.yml. The CLI's own error text is
  # appended because it is the part that says which.
  payload="$(databricks model-versions get-by-alias "${full_name}" "${alias_name}" -o json 2>&1)" ||
    fail "could not resolve alias ${alias_name} on ${full_name}: ${payload}"

  version="$(echo "${payload}" | jq -r '.version // empty' 2>/dev/null)"

  assert_numeric_version "${version}" "${alias_name} version on ${full_name}"

  # ONLY the number on stdout: this is what the pipeline captures.
  echo "${version}"
}

# --- verify-version ---------------------------------------------------------
cmd_verify_version() {
  require_args 2 $# \
    "model-promotion.sh verify-version FULL_NAME VERSION --environment ENV [--git-sha SHA] [--require-ready]"

  local full_name="$1" version="$2"
  shift 2

  local environment="" git_sha="" require_ready="no"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --environment) environment="${2-}"; shift 2 ;;
      --git-sha)     git_sha="${2-}";     shift 2 ;;
      --require-ready) require_ready="yes"; shift ;;
      *) fail "unknown option '$1'" ;;
    esac
  done

  assert_numeric_version "${version}" "requested version"
  [[ -n "${environment}" ]] || fail "--environment is required"

  local payload
  payload="$(model_version_json "${full_name}" "${version}")" || exit 1

  # The version really is the one asked for. Guards against a CLI that resolved
  # something else, and makes the assertion explicit in the build log.
  local actual_version
  actual_version="$(echo "${payload}" | jq -r '.version // empty')"
  assert_numeric_version "${actual_version}" "returned version"

  [[ "${actual_version}" == "${version}" ]] ||
    fail "asked for version ${version} but got ${actual_version}"

  # catalog_name IS the environment, and unlike a tag it is structural: it comes
  # from where the artefact lives, not from what training claimed about it. It
  # is checked first for that reason.
  local catalog_name
  catalog_name="$(echo "${payload}" | jq -r '.catalog_name // empty')"

  [[ -n "${catalog_name}" ]] || fail "model version returned no catalog_name"
  [[ "${catalog_name}" == "${environment}" ]] ||
    fail "version ${version} lives in catalog '${catalog_name}', expected '${environment}'"

  if [[ "${require_ready}" == "yes" ]]; then
    local status
    status="$(echo "${payload}" | jq -r '.status // empty')"

    [[ -n "${status}" ]] || fail "model version returned no status"
    [[ "${status}" == "READY" ]] ||
      fail "version ${version} status is '${status}', expected READY"
  fi

  # The environment TAG is checked as well as the catalog: a mismatch between
  # where the artefact lives and what training recorded means something is
  # wrong, even though the catalog is authoritative.
  local tag_environment
  tag_environment="$(echo "${payload}" | jq -r "${TAG_LOOKUP} tagval(\"environment\")")"

  if [[ -n "${tag_environment}" && "${tag_environment}" != "${environment}" ]]; then
    fail "version ${version} is tagged environment='${tag_environment}', expected '${environment}'"
  fi

  local run_id
  run_id="$(echo "${payload}" | jq -r '.run_id // empty')"

  if [[ -n "${git_sha}" ]]; then
    local actual_sha
    actual_sha="$(echo "${payload}" | jq -r "${TAG_LOOKUP} tagval(\"git_sha\")")"

    # Fall back to the MLflow RUN's tags when the model version carries none.
    # UC returns model-version tags only when set and readable, whereas the
    # training run's tags are always written; without this fallback a readable
    # provenance record could still read as "absent" and block a valid release.
    if [[ -z "${actual_sha}" && -n "${run_id}" ]]; then
      local run_payload
      run_payload="$(databricks experiments get-run "${run_id}" -o json 2>/dev/null)" || run_payload=""

      if [[ -n "${run_payload}" ]]; then
        actual_sha="$(echo "${run_payload}" | jq -r ".run.data | ${TAG_LOOKUP} tagval(\"git_sha\")" 2>/dev/null)"
      fi
    fi

    [[ -n "${actual_sha}" ]] ||
      fail "version ${version} records no git_sha, but commit ${git_sha} was expected"

    [[ "${actual_sha}" == "${git_sha}" ]] ||
      fail "version ${version} was trained from commit ${actual_sha}, expected ${git_sha}"
  fi

  echo "PASS: ${full_name} version ${version} verified"
  echo "  catalog:     ${catalog_name}"
  echo "  environment: ${environment}"
  [[ -n "${run_id}" ]] && echo "  run_id:      ${run_id}"
  [[ -n "${git_sha}" ]] && echo "  git_sha:     ${git_sha}"
  [[ "${require_ready}" == "yes" ]] && echo "  status:      READY"

  return 0
}

# --- describe-version -------------------------------------------------------
#
# Candidate evidence for a human approver, printed into the build log before the
# approval is requested. Read-only.
cmd_describe_version() {
  require_args 2 $# "model-promotion.sh describe-version FULL_NAME VERSION"
  local full_name="$1" version="$2" payload

  assert_numeric_version "${version}" "requested version"
  payload="$(model_version_json "${full_name}" "${version}")" || exit 1

  echo "Candidate evidence for ${full_name} version ${version}:"
  echo "${payload}" | jq '{
      catalog_name,
      schema_name,
      model_name,
      version,
      status,
      run_id,
      source,
      storage_location,
      tags
    }'
}

# --- promote ----------------------------------------------------------------
#
# The only mutating subcommand. Moves Champion to ONE EXACT version — never to
# "whatever Candidate points at now", which is the distinction the PROD approval
# depends on.
cmd_promote() {
  require_args 2 $# "model-promotion.sh promote FULL_NAME VERSION"
  local full_name="$1" version="$2" output

  assert_numeric_version "${version}" "version to promote"

  output="$(databricks registered-models set-alias "${full_name}" Champion "${version}" 2>&1)" ||
    fail "could not set Champion on ${full_name} version ${version}: ${output}"

  echo "Champion -> ${full_name} version ${version}"
}

# --- verify-alias -----------------------------------------------------------
cmd_verify_alias() {
  require_args 3 $# "model-promotion.sh verify-alias FULL_NAME ALIAS VERSION"
  local full_name="$1" alias_name="$2" expected="$3" actual

  assert_numeric_version "${expected}" "expected version"

  actual="$(cmd_resolve_alias "${full_name}" "${alias_name}")" || exit 1

  [[ "${actual}" == "${expected}" ]] ||
    fail "${alias_name} points to version ${actual}, expected ${expected}"

  echo "PASS: ${alias_name} -> ${full_name} version ${expected}"
}

# --- write-manifest ---------------------------------------------------------
#
# The release manifest: an IMMUTABLE record of exactly which artefact was
# evaluated, written before any approval is requested and published as a
# pipeline artifact.
#
# This replaces passing the version through a stage output variable. A variable
# is invisible after the run, is easy to get wrong in an expression, and proves
# nothing about what the approver saw. An artifact is content-addressable
# evidence attached to this specific run: the release stage downloads it, and if
# it is absent or disagrees with the workspace, the release stops. Crucially,
# neither path re-resolves the mutable Candidate alias.
#
# The manifest is only written AFTER the version passes full verification, so it
# can never contain an unverified claim.
cmd_write_manifest() {
  require_args 2 $# \
    "model-promotion.sh write-manifest FULL_NAME VERSION --environment ENV --git-sha SHA --output PATH"

  local full_name="$1" version="$2"
  shift 2

  local environment="" git_sha="" output=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --environment) environment="${2-}"; shift 2 ;;
      --git-sha)     git_sha="${2-}";     shift 2 ;;
      --output)      output="${2-}";      shift 2 ;;
      *) fail "unknown option '$1'" ;;
    esac
  done

  [[ -n "${output}" ]] || fail "--output is required"
  [[ -n "${git_sha}" ]] || fail "--git-sha is required"

  # Full verification first. A manifest is evidence; evidence that was never
  # checked is worse than none, because it looks authoritative.
  cmd_verify_version "${full_name}" "${version}" \
    --environment "${environment}" --git-sha "${git_sha}" --require-ready || exit 1

  local payload
  payload="$(model_version_json "${full_name}" "${version}")" || exit 1

  mkdir -p "$(dirname "${output}")" ||
    fail "could not create the directory for ${output}"

  echo "${payload}" | jq \
    --arg product "${PRODUCT_NAME}" \
    --arg full_model_name "${full_name}" \
    --arg git_sha "${git_sha}" \
    '{
      product:          $product,
      full_model_name:  $full_model_name,
      catalog:          .catalog_name,
      schema:           .schema_name,
      candidate_version: (.version | tostring),
      git_sha:          $git_sha,
      run_id:           (.run_id // ""),
      source:           (.source // ""),
      status:           .status
    }' > "${output}" ||
    fail "could not write the release manifest to ${output}"

  # Read it back through the same validator the release stage will use. If the
  # manifest we just wrote would not pass verification, that is a bug here, and
  # it must surface now rather than after an approver has waited.
  cmd_verify_manifest "${output}" \
    --model "${full_name}" --environment "${environment}" --git-sha "${git_sha}" >/dev/null || exit 1

  echo "Wrote release manifest to ${output}:"
  cat "${output}"
}

# --- verify-manifest --------------------------------------------------------
#
# Validate a downloaded manifest and print ONLY the candidate version, so the
# release stage can capture it with $(...). Fails closed on every field.
cmd_verify_manifest() {
  require_args 1 $# \
    "model-promotion.sh verify-manifest PATH --model FULL_NAME --environment ENV --git-sha SHA"

  local path="$1"
  shift

  local full_name="" environment="" git_sha=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --model)       full_name="${2-}";   shift 2 ;;
      --environment) environment="${2-}"; shift 2 ;;
      --git-sha)     git_sha="${2-}";     shift 2 ;;
      *) fail "unknown option '$1'" ;;
    esac
  done

  [[ -n "${full_name}" ]] || fail "--model is required"
  [[ -n "${environment}" ]] || fail "--environment is required"
  [[ -n "${git_sha}" ]] || fail "--git-sha is required"

  [[ -f "${path}" ]] || fail "release manifest not found at ${path}"

  jq -e . "${path}" >/dev/null 2>&1 || fail "release manifest at ${path} is not valid JSON"

  local field
  for field in product full_model_name catalog schema candidate_version git_sha status; do
    local value
    value="$(jq -r --arg f "${field}" '.[$f] // empty' "${path}")"
    [[ -n "${value}" ]] || fail "release manifest is missing '${field}'"
  done

  local m_product m_model m_catalog m_schema m_version m_sha m_status
  m_product="$(jq -r '.product' "${path}")"
  m_model="$(jq -r '.full_model_name' "${path}")"
  m_catalog="$(jq -r '.catalog' "${path}")"
  m_schema="$(jq -r '.schema' "${path}")"
  m_version="$(jq -r '.candidate_version' "${path}")"
  m_sha="$(jq -r '.git_sha' "${path}")"
  m_status="$(jq -r '.status' "${path}")"

  [[ "${m_product}" == "${PRODUCT_NAME}" ]] ||
    fail "release manifest is for product '${m_product}', expected '${PRODUCT_NAME}'"

  [[ "${m_model}" == "${full_name}" ]] ||
    fail "release manifest names model '${m_model}', expected '${full_name}'"

  # The manifest's own three-level name must agree with its catalog and schema
  # fields, so a hand-edited manifest cannot claim a PROD model while pointing
  # at another catalog.
  [[ "${m_model}" == "${m_catalog}.${m_schema}."* ]] ||
    fail "release manifest model '${m_model}' does not match catalog '${m_catalog}' and schema '${m_schema}'"

  [[ "${m_catalog}" == "${environment}" ]] ||
    fail "release manifest is for catalog '${m_catalog}', expected '${environment}'"

  assert_numeric_version "${m_version}" "release manifest candidate_version"

  [[ "${m_status}" == "READY" ]] ||
    fail "release manifest records status '${m_status}', expected READY"

  [[ "${m_sha}" == "${git_sha}" ]] ||
    fail "release manifest is for commit ${m_sha}, expected ${git_sha}"

  {
    echo "PASS: release manifest verified"
    echo "  product:  ${m_product}"
    echo "  model:    ${m_model}"
    echo "  catalog:  ${m_catalog}"
    echo "  version:  ${m_version}"
    echo "  git_sha:  ${m_sha}"
    echo "  status:   ${m_status}"
  } >&2

  # ONLY the number on stdout: this is what the release stage captures.
  echo "${m_version}"
}

main() {
  [[ $# -ge 1 ]] || fail "usage: model-promotion.sh <resolve-alias|verify-version|describe-version|write-manifest|verify-manifest|promote|verify-alias> ..."

  local command="$1"
  shift

  case "${command}" in
    resolve-alias)    cmd_resolve_alias "$@" ;;
    verify-version)   cmd_verify_version "$@" ;;
    describe-version) cmd_describe_version "$@" ;;
    write-manifest)   cmd_write_manifest "$@" ;;
    verify-manifest)  cmd_verify_manifest "$@" ;;
    promote)          cmd_promote "$@" ;;
    verify-alias)     cmd_verify_alias "$@" ;;
    *) fail "unknown subcommand '${command}'" ;;
  esac
}

main "$@"
