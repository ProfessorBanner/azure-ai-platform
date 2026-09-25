variable "subscription_id" {
  description = "Azure subscription ID. Supplied at runtime (TF_VAR_subscription_id or a git-ignored terraform.tfvars); never committed. Must equal the controls root's subscription."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.subscription_id))
    error_message = "subscription_id must be a lowercase GUID."
  }
}

variable "location" {
  description = "Azure region; must match the region discovery priced and quota-checked."
  type        = string
  default     = "uksouth"

  validation {
    condition     = contains(["uksouth", "ukwest"], var.location)
    error_message = "location must be uksouth (default) or ukwest."
  }
}

variable "project" {
  description = "Project identity; must equal the controls root's project tag or the watchdog will never consider the resources eligible."
  type        = string
  default     = "pet-classifier-aks-mlops"
}

variable "owner" {
  description = "Owner tag value (not an email address)."
  type        = string
  default     = "platform-engineering"
}

variable "session_id" {
  description = "Immutable session id from the ledger reservation (sYYYYMMDD-HHMM-xxxxxx). Stamped on every resource; must equal the session the controls root armed."
  type        = string

  validation {
    condition     = can(regex("^s[0-9]{8}-[0-9]{4}-[a-z0-9]{6}$", var.session_id))
    error_message = "session_id must look like sYYYYMMDD-HHMM-xxxxxx."
  }
}

variable "expiry_utc" {
  description = "Session expiry (RFC 3339 UTC) from the reservation; must equal the armed expiry. Defined from the reservation/start of provisioning, never from when training becomes ready."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.expiry_utc))
    error_message = "expiry_utc must be RFC 3339 UTC with a trailing Z."
  }
}

variable "session_resource_group_name" {
  description = "The EXISTING empty session group created by the controls root. This root never creates or destroys it."
  type        = string
  default     = "rg-aiplatform-aksmlops-session"
}

variable "node_resource_group_name" {
  description = "Explicit AKS node resource group name; must equal the controls root's session_node_resource_group_name so the watchdog and budget cover it."
  type        = string
  default     = "rg-aiplatform-aksmlops-session-nodes"

  validation {
    condition     = can(regex("^rg-aiplatform-aksmlops-session-nodes[a-z0-9-]*$", var.node_resource_group_name))
    error_message = "node_resource_group_name must start with rg-aiplatform-aksmlops-session-nodes."
  }
}

variable "retained_storage_account_id" {
  description = "Resource ID of the retained storage account (controls output). The kubelet identity is granted Storage Blob Data Reader on it so training can stage inputs. Scope must exist at apply."
  type        = string

  validation {
    condition     = can(regex("^/subscriptions/[0-9a-f-]{36}/resourceGroups/rg-aiplatform-aksmlops-retained[a-z0-9-]*/providers/Microsoft.Storage/storageAccounts/[a-z0-9]{3,24}$", var.retained_storage_account_id))
    error_message = "retained_storage_account_id must be a storage account resource ID inside rg-aiplatform-aksmlops-retained."
  }
}

variable "cluster_admin_principal_id" {
  description = "Object ID of the operator (Entra user) granted Azure Kubernetes Service RBAC Cluster Admin on this cluster. Local accounts are disabled, so this is the only way in. Supplied at runtime; never committed."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.cluster_admin_principal_id))
    error_message = "cluster_admin_principal_id must be a lowercase GUID."
  }
}

variable "kubernetes_version" {
  description = "Pinned AKS version. 1.35 is the current default in UK South (discovery 2026-09-17); patch pinned so an upgrade is a reviewed change."
  type        = string
  default     = "1.35.7"

  validation {
    condition     = can(regex("^1\\.(34|35|36)\\.[0-9]+$", var.kubernetes_version))
    error_message = "kubernetes_version must be a 1.34.x, 1.35.x or 1.36.x patch version (the supported non-LTS lines in UK South at discovery time)."
  }
}

variable "system_node_vm_size" {
  description = "System node SKU. Standard_D4als_v6 (4 vCPU, 8 GiB, x86-64) meets the documented system-pool minimum of 4 vCPU / 4 GB, is not B-series, has quota in the Dalv6 family (10 vCPU) and was the cheapest such SKU priced in UK South."
  type        = string
  default     = "Standard_D4als_v6"

  validation {
    condition     = contains(["Standard_D4als_v6", "Standard_D4ls_v6", "Standard_D4as_v6"], var.system_node_vm_size)
    error_message = "system_node_vm_size must be one of the priced, quota-holding 4 vCPU SKUs: Standard_D4als_v6, Standard_D4ls_v6, Standard_D4as_v6."
  }
}

variable "system_node_count" {
  description = "Fixed system node count. 1 for the lab (documented deviation from the production recommendation of 2+); 2 is allowed if AKS rejects a single node at creation."
  type        = number
  default     = 1

  validation {
    condition     = var.system_node_count >= 1 && var.system_node_count <= 2
    error_message = "system_node_count must be 1 or 2."
  }
}

variable "gpu_node_pool_enabled" {
  description = "Whether the single GPU user node pool exists. FALSE in the initial deployment stage; G4 flips it only after live watchdog verification, quota and admission all pass."
  type        = bool
  default     = false
}

variable "gpu_node_vm_size" {
  description = "GPU node SKU. Standard_NC4as_T4_v3 (4 vCPU, 28 GiB, 1x T4, x86-64) is the documented minimum GPU size for AKS node pools."
  type        = string
  default     = "Standard_NC4as_T4_v3"

  validation {
    condition     = contains(["Standard_NC4as_T4_v3"], var.gpu_node_vm_size)
    error_message = "gpu_node_vm_size must be Standard_NC4as_T4_v3 (the only priced and reviewed GPU SKU)."
  }
}

variable "gpu_node_count" {
  description = "Fixed GPU node count; at most 1 (budget policy max_gpu_nodes). No autoscaling."
  type        = number
  default     = 1

  validation {
    condition     = var.gpu_node_count >= 1 && var.gpu_node_count <= 1
    error_message = "gpu_node_count must be exactly 1 (policy maximum)."
  }
}

variable "os_disk_size_gb" {
  description = "Managed OS disk size for every node. 64 GB maps to a P6 premium disk (USD 12.35/month retail, billed hourly) and leaves room for a multi-GB CUDA image."
  type        = number
  default     = 64

  validation {
    condition     = contains([32, 64, 128], var.os_disk_size_gb)
    error_message = "os_disk_size_gb must be 32, 64 or 128 (P4, P6 or P10 price bands)."
  }
}

variable "container_registry_name" {
  description = "Override for the session ACR name (5-50 lowercase alphanumerics). Leave null to derive acraksmlops<8 hex chars of the session id hash>."
  type        = string
  default     = null

  validation {
    condition     = var.container_registry_name == null || can(regex("^[a-z0-9]{5,50}$", var.container_registry_name))
    error_message = "container_registry_name must be 5-50 lowercase alphanumeric characters."
  }
}

variable "api_server_authorized_ip_cidrs" {
  description = "IPv4 addresses or CIDR ranges allowed to reach the Kubernetes API server (kubectl, port-forward). Supplied at runtime (operator egress address); never committed. AKS adds the cluster's own outbound IP automatically."
  type        = list(string)

  validation {
    condition     = length(var.api_server_authorized_ip_cidrs) > 0
    error_message = "api_server_authorized_ip_cidrs must list at least one address or CIDR."
  }
}
