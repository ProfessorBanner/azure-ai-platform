# PLATFORM MODE — the single committed source of truth for the stg environment.
#
# This file is what Azure DevOps consumes: the Terraform CI plan, the CD plan,
# the exact-plan apply and the post-apply drift check all read the committed
# `default` below from the checked-out `main` commit. No pipeline parameter,
# TF_VAR or .tfvars supplies this value (scripts/ci/terraform-plan.sh and
# terraform-cd-drift-check.sh refuse to run if TF_VAR_idle_mode is set), and
# scripts/ci/check-platform-mode.sh reads this same line to block product
# deployments into an idle environment.
#
# Change the default only via a reviewed commit. Procedure:
# docs/runbooks/platform-idle-mode.md. Decision: ADR 0013.
#
#   default = false  -> ACTIVE: NAT gateway attached, storage private endpoints present.
#   default = true   -> IDLE:   NAT gateway + associations and the dfs/blob private
#                              endpoints are removed; everything else is retained.
variable "idle_mode" {
  description = "Persistent platform mode for this environment (false = active, true = idle). The committed default IS the mode; see platform_mode.tf header and docs/runbooks/platform-idle-mode.md."
  type        = bool
  default     = true
}
