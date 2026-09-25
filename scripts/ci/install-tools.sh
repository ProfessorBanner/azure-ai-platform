#!/usr/bin/env bash
#
# Install pinned Terraform, TFLint, Trivy and uv for the Terraform CI pipeline.
#
# Non-destructive: downloads pinned release archives into a workspace-local bin
# directory and prepends it to PATH for subsequent pipeline steps (via the
# Azure Pipelines `##vso[task.prependpath]` logging command). It never touches
# any Azure or Terraform state.
#
# Required environment variables (set by the pipeline):
#   TERRAFORM_VERSION, TFLINT_VERSION, TRIVY_VERSION  — exact versions to pin.
# Optional:
#   TOOLS_BIN  — target directory for the binaries (default: a temp dir).
#
set -euo pipefail

TERRAFORM_VERSION="${TERRAFORM_VERSION:?TERRAFORM_VERSION must be set}"
TFLINT_VERSION="${TFLINT_VERSION:?TFLINT_VERSION must be set}"
TRIVY_VERSION="${TRIVY_VERSION:?TRIVY_VERSION must be set}"
UV_VERSION="${UV_VERSION:-0.12.0}"
TOOLS_BIN="${TOOLS_BIN:-${AGENT_TEMPDIRECTORY:-/tmp}/tf-ci-tools/bin}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

arch="linux_amd64"
mkdir -p "${TOOLS_BIN}"
workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

echo "==> Installing Terraform ${TERRAFORM_VERSION}"
curl -fsSL -o "${workdir}/terraform.zip" \
  "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_${arch}.zip" \
  || fail "failed to download Terraform ${TERRAFORM_VERSION}"
unzip -o -q "${workdir}/terraform.zip" -d "${TOOLS_BIN}" || fail "failed to unpack Terraform"

echo "==> Installing TFLint ${TFLINT_VERSION}"
curl -fsSL -o "${workdir}/tflint.zip" \
  "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_${arch}.zip" \
  || fail "failed to download TFLint ${TFLINT_VERSION}"
unzip -o -q "${workdir}/tflint.zip" -d "${TOOLS_BIN}" || fail "failed to unpack TFLint"

echo "==> Installing Trivy ${TRIVY_VERSION}"
curl -fsSL -o "${workdir}/trivy.tar.gz" \
  "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
  || fail "failed to download Trivy ${TRIVY_VERSION}"
tar -xzf "${workdir}/trivy.tar.gz" -C "${TOOLS_BIN}" trivy || fail "failed to unpack Trivy"
chmod +x "${TOOLS_BIN}/trivy"
echo "==> Verifying Trivy ${TRIVY_VERSION}"
"${TOOLS_BIN}/trivy" --version || fail "Trivy verification failed after installation"

echo "==> Installing uv ${UV_VERSION}"
curl -fsSL -o "${workdir}/uv.tar.gz"   "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"   || fail "failed to download uv ${UV_VERSION}"

tar -xzf "${workdir}/uv.tar.gz" -C "${workdir}"   || fail "failed to unpack uv"

cp "${workdir}/uv-x86_64-unknown-linux-gnu/uv" "${TOOLS_BIN}/uv"   || fail "failed to install uv"

chmod +x "${TOOLS_BIN}/terraform" "${TOOLS_BIN}/tflint" "${TOOLS_BIN}/trivy" "${TOOLS_BIN}/uv"

# Expose the pinned tools to subsequent pipeline steps.
echo "##vso[task.prependpath]${TOOLS_BIN}"

echo "==> Verifying installed versions"
# Capture Terraform's full version output first, then take the first line with
# shell parameter expansion. Never pipe the Terraform process straight into
# `head`: under `set -o pipefail`, head closing the pipe early sends Terraform
# SIGPIPE (exit 141), which pipefail would surface as a spurious step failure.
tf_version_output="$("${TOOLS_BIN}/terraform" version)"
tf_version_line="${tf_version_output%%$'\n'*}"
installed_tf="$(awk '{print $2}' <<<"${tf_version_line}" | tr -d 'v')"
[[ "${installed_tf}" == "${TERRAFORM_VERSION}" ]] \
  || fail "Terraform version mismatch: found ${installed_tf}, expected ${TERRAFORM_VERSION}"
printf '%s\n' "${tf_version_line}"
"${TOOLS_BIN}/tflint" --version
"${TOOLS_BIN}/trivy" --version
"${TOOLS_BIN}/uv" --version

echo "Tool installation complete: ${TOOLS_BIN}"
