variable "subscription_id" {
  description = "Azure subscription ID the controls are deployed into. Supplied at runtime (TF_VAR_subscription_id or a git-ignored terraform.tfvars); never committed. Every apply and every watchdog decision is pinned to this exact value."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.subscription_id))
    error_message = "subscription_id must be a lowercase GUID."
  }
}

variable "location" {
  description = "Azure region. UK South is the platform default and the region discovery priced and quota-checked."
  type        = string
  default     = "uksouth"

  validation {
    condition     = contains(["uksouth", "ukwest"], var.location)
    error_message = "location must be uksouth (default) or ukwest (secondary, only when justified)."
  }
}

variable "project" {
  description = "Project identity stamped on every resource and required by the watchdog before anything is eligible for deletion."
  type        = string
  default     = "pet-classifier-aks-mlops"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,40}$", var.project))
    error_message = "project must be lowercase alphanumeric with hyphens, 3-41 characters."
  }
}

variable "owner" {
  description = "Owner tag value (a person or team identifier, not an email address, so no personal data is committed)."
  type        = string
  default     = "platform-engineering"
}

variable "controls_expiry_utc" {
  description = "Expiry tag for the CONTROL resources themselves (RFC 3339 UTC, e.g. 2026-12-31T00:00:00Z). Control resources are cheap but not free of review: this date is when the whole capability should be re-justified or removed."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.controls_expiry_utc))
    error_message = "controls_expiry_utc must be RFC 3339 UTC with a trailing Z, e.g. 2026-12-31T00:00:00Z."
  }
}

# --- Session allowlist ---------------------------------------------------------
#
# The session resource group is created HERE, empty, so that the watchdog's
# role assignment can be scoped to it before any compute exists. This is what
# breaks the cycle "cleanup protection cannot be configured until the GPU
# cluster exists". The session root deploys INTO this group and never creates
# or destroys it.

variable "session_resource_group_name" {
  description = "Name of the single disposable session resource group. The watchdog may delete resources ONLY inside this exact group."
  type        = string
  default     = "rg-aiplatform-aksmlops-session"

  validation {
    condition     = can(regex("^rg-aiplatform-aksmlops-session[a-z0-9-]*$", var.session_resource_group_name))
    error_message = "session_resource_group_name must start with rg-aiplatform-aksmlops-session."
  }
}

variable "session_node_resource_group_name" {
  description = "Explicit name of the AKS-managed node resource group the session root will request. Recorded here so the watchdog can re-inventory it after deletion and so budgets can include it."
  type        = string
  default     = "rg-aiplatform-aksmlops-session-nodes"

  validation {
    condition     = can(regex("^rg-aiplatform-aksmlops-session-nodes[a-z0-9-]*$", var.session_node_resource_group_name))
    error_message = "session_node_resource_group_name must start with rg-aiplatform-aksmlops-session-nodes."
  }
}

# --- Armed session -------------------------------------------------------------

variable "armed_session" {
  description = <<-EOT
    The ONE session whose deadline is armed. null (default) arms nothing: the
    sweep still runs and reports, but no session tags exist for it to match.
    Arming is G4 stage 3 and precedes provisioning compute (stage 4).
      session_id  - immutable id, e.g. s20261001-1200-ab12cd; matches the
                    ledger and the tags the session root stamps.
      expiry_utc  - RFC 3339 UTC; defined from the session reservation, not
                    from when training becomes ready.
  EOT
  type = object({
    session_id = string
    expiry_utc = string
  })
  default = null

  validation {
    condition = var.armed_session == null || (
      can(regex("^s[0-9]{8}-[0-9]{4}-[a-z0-9]{6}$", var.armed_session.session_id)) &&
      can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.armed_session.expiry_utc))
    )
    error_message = "armed_session.session_id must look like sYYYYMMDD-HHMM-xxxxxx and expiry_utc must be RFC 3339 UTC with a trailing Z."
  }
}

variable "watchdog_mode" {
  description = "Runbook mode passed by both schedules. \"Report\" (default) inventories and logs what WOULD be deleted. \"Execute\" deletes eligible resources. G3 never applies Execute; G4 switches it only after the report-only path is proven live."
  type        = string
  default     = "Report"

  validation {
    condition     = contains(["Report", "Execute"], var.watchdog_mode)
    error_message = "watchdog_mode must be Report or Execute."
  }
}

variable "sweep_start_time_utc" {
  description = "First run of the hourly sweep schedule (RFC 3339 UTC). Azure requires a schedule start at least five minutes in the future at creation, so this is supplied at apply time rather than defaulted."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.sweep_start_time_utc))
    error_message = "sweep_start_time_utc must be RFC 3339 UTC with a trailing Z."
  }
}

variable "automation_public_network_access_enabled" {
  description = "Inbound public access to the Automation account endpoints (webhooks, hybrid workers). Cloud sandbox jobs call ARM outbound and do not need it, so it is off. Flip only if G4 live verification shows the job runtime needs it."
  type        = bool
  default     = false
}

# --- Retained storage ------------------------------------------------------------

variable "retained_resource_group_name" {
  description = "Resource group for RETAINED artifacts (immutable inputs staged for training, exported model packages, watchdog results). Outside the session deletion scope; costed and expiring separately."
  type        = string
  default     = "rg-aiplatform-aksmlops-retained"

  validation {
    condition     = can(regex("^rg-aiplatform-aksmlops-retained[a-z0-9-]*$", var.retained_resource_group_name))
    error_message = "retained_resource_group_name must start with rg-aiplatform-aksmlops-retained."
  }
}

variable "retained_blob_expiry_days" {
  description = "Blobs in the retained account are deleted this many days after last modification by a storage lifecycle rule. This is the explicit cleanup policy for retained data."
  type        = number
  default     = 60

  validation {
    condition     = var.retained_blob_expiry_days >= 7 && var.retained_blob_expiry_days <= 365
    error_message = "retained_blob_expiry_days must be between 7 and 365."
  }
}

variable "retained_storage_account_name" {
  description = "Override for the globally unique storage account name. Leave null to derive staksmlops<8 hex chars of the subscription id hash>."
  type        = string
  default     = null

  validation {
    condition     = var.retained_storage_account_name == null || can(regex("^[a-z0-9]{3,24}$", var.retained_storage_account_name))
    error_message = "retained_storage_account_name must be 3-24 lowercase alphanumeric characters."
  }
}

# --- Budget policy (mirrors deploy/g3/budget-policy.v1.json) ---------------------

variable "budget_policy" {
  description = "The versioned budget policy, repeated here so Terraform validates the same numbers the Python admission code enforces. project_limit is the lifetime USD total; admission_limit is the planned ceiling; contingency is never available to ordinary admission."
  type = object({
    version             = string
    project_limit_usd   = number
    admission_limit_usd = number
    contingency_usd     = number
    max_gpu_nodes       = number
  })
  default = {
    version             = "1"
    project_limit_usd   = 100
    admission_limit_usd = 60
    contingency_usd     = 40
    max_gpu_nodes       = 1
  }

  validation {
    condition = (
      var.budget_policy.admission_limit_usd + var.budget_policy.contingency_usd == var.budget_policy.project_limit_usd &&
      var.budget_policy.admission_limit_usd > 0 &&
      var.budget_policy.max_gpu_nodes >= 0 && var.budget_policy.max_gpu_nodes <= 1
    )
    error_message = "budget_policy must satisfy admission_limit_usd + contingency_usd == project_limit_usd and 0 <= max_gpu_nodes <= 1."
  }
}

# --- Optional Azure budget alert (supplementary signal, not a control) -----------

variable "budget_alert" {
  description = <<-EOT
    Optional Azure Cost Management budget. Disabled by default because a budget
    needs a real notification recipient and none is invented here. When enabled:
      contact_emails - explicit recipients (supplied at runtime, never committed);
      start_date     - first day of the month the budget starts (RFC 3339 UTC);
      end_date       - when the budget stops evaluating.
    The budget covers the session, node, controls and retained groups. Its
    monthly period resets; the lifetime ledger does not.
  EOT
  type = object({
    enabled        = bool
    contact_emails = list(string)
    start_date     = string
    end_date       = string
  })
  default = {
    enabled        = false
    contact_emails = []
    start_date     = "2026-10-01T00:00:00Z"
    end_date       = "2027-09-30T00:00:00Z"
  }

  validation {
    condition     = !var.budget_alert.enabled || length(var.budget_alert.contact_emails) > 0
    error_message = "budget_alert.contact_emails must name at least one recipient when the budget is enabled."
  }
}

variable "allowed_ip_cidrs" {
  description = "IPv4 addresses or CIDR ranges allowed to reach the retained storage account (default action Deny). Supplied at runtime (the operator's egress address; in G4 also the session cluster's outbound IP); never committed. Azure Automation sandbox addresses cannot be listed, so the watchdog's result-blob write is best-effort and the job history is the authoritative record."
  type        = list(string)

  validation {
    condition     = length(var.allowed_ip_cidrs) > 0 && alltrue([for c in var.allowed_ip_cidrs : can(cidrhost(c, 0)) || can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}$", c))])
    error_message = "allowed_ip_cidrs must list at least one IPv4 address or CIDR."
  }
}
