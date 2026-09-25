# AKS MLOps cost controls — SESSION root (disposable)
# (state key: capabilities/aks-mlops/session.tfstate).
#
# One temporary AKS lab per session, deployed INTO the empty session resource
# group the controls root owns. Everything here carries lifecycle=disposable
# and the session id, which is exactly what the expiry watchdog matches on.
#
#   - AKS Free tier (no control-plane charge, no uptime SLA — a lab).
#   - One fixed x86-64 system node (Standard_D4als_v6). Application pods run
#     on it in the initial stage; there is no user CPU pool.
#   - Optional single T4 GPU user node, DISABLED by default, fixed count 1,
#     no autoscaling, Regular priority (never Spot).
#   - Outbound via the AKS default Standard Load Balancer with one managed
#     public IP: this is the cheapest supported egress path (the `none`
#     outbound type requires a network-isolated cluster) and it is PRICED —
#     port-forward removes ingress, not the egress infrastructure.
#   - A Basic container registry for image transport, deleted with the session.
#   - No ingress, no NodePort, no monitoring add-on, no managed database.
#
# This root never creates a resource group: the session group comes from the
# controls root (referenced by NAME so a plan is possible before it exists; an
# apply fails fast if it is missing) and AKS creates the node group itself.

locals {
  session_tags = {
    project     = var.project
    environment = "sandbox"
    owner       = var.owner
    platform    = "azure-ai-platform"
    managed_by  = "terraform"
    capability  = "aks-mlops"
    lifecycle   = "disposable"
    session_id  = var.session_id
    expiry_utc  = var.expiry_utc
  }

  cluster_name   = "aks-aiplatform-aksmlops-session"
  registry_name  = coalesce(var.container_registry_name, "acraksmlops${substr(sha256(var.session_id), 0, 8)}")
  gpu_pool_count = var.gpu_node_pool_enabled ? 1 : 0
}

data "azurerm_client_config" "current" {}

# --- Cluster -----------------------------------------------------------------------

resource "azurerm_kubernetes_cluster" "lab" {
  name                = local.cluster_name
  location            = var.location
  resource_group_name = var.session_resource_group_name
  dns_prefix          = "aksmlops-${substr(sha256(var.session_id), 0, 6)}"
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free"
  support_plan        = "KubernetesOfficial"
  node_resource_group = var.node_resource_group_name

  # Entra ID only: no local admin kubeconfig can be issued.
  local_account_disabled = true
  # No automatic_upgrade_channel: omitted means no auto-upgrade of the control plane.
  run_command_enabled     = false
  node_os_upgrade_channel = "NodeImage"

  identity {
    type = "SystemAssigned"
  }

  # The API server answers only the operator's address; there is no other
  # client. AKS adds the cluster's own egress IP to the list itself.
  api_server_access_profile {
    authorized_ip_ranges = var.api_server_authorized_ip_cidrs
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = var.system_node_vm_size
    node_count                   = var.system_node_count
    auto_scaling_enabled         = false
    os_disk_type                 = "Managed"
    os_disk_size_gb              = var.os_disk_size_gb
    os_sku                       = "Ubuntu"
    max_pods                     = 30
    only_critical_addons_enabled = false
    temporary_name_for_rotation  = "systemtmp"

    upgrade_settings {
      max_surge = "1"
    }

    tags = local.session_tags
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    # NetworkPolicy enforcement so the lab can restrict pod traffic (G2 noted
    # kind's CNI could not); the Azure policy engine adds no cost.
    network_policy    = "azure"
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"

    load_balancer_profile {
      managed_outbound_ip_count = 1
    }
  }

  tags = local.session_tags

  lifecycle {
    precondition {
      condition     = var.system_node_count == 1 || var.system_node_count == 2
      error_message = "Fixed system node count only: 1 (lab default) or 2."
    }
  }
}

# --- Optional single T4 GPU user node -------------------------------------------------
#
# AKS installs the NVIDIA driver and device plugin (the recommended managed
# path) on the default Ubuntu node image. The pool is tainted so only the
# training Job that tolerates sku=gpu lands on it. The G2 CPU-only image cannot
# use this node: G4 builds a separate linux/amd64 CUDA training image.
resource "azurerm_kubernetes_cluster_node_pool" "gpu" {
  count = local.gpu_pool_count

  name                  = "gpu"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.lab.id
  mode                  = "User"
  vm_size               = var.gpu_node_vm_size
  node_count            = var.gpu_node_count
  auto_scaling_enabled  = false
  priority              = "Regular"
  os_type               = "Linux"
  os_sku                = "Ubuntu"
  os_disk_type          = "Managed"
  os_disk_size_gb       = var.os_disk_size_gb
  max_pods              = 30
  gpu_driver            = "Install"

  node_taints = ["sku=gpu:NoSchedule"]
  node_labels = {
    "aksmlops.pet-classifier/gpu" = "t4"
  }

  tags = local.session_tags
}

# --- Image transport: Basic registry, deleted with the session -------------------------
#
# Cheapest workable transport for a ~2 GB image: USD 0.1666 per day retail plus
# USD 0.10 per GB-month of storage, which for a session measured in hours is
# cents. Admin account off; the operator pushes with `az acr login` (Entra
# token) and the kubelet identity pulls through AcrPull below. Basic has no
# network rules, so the endpoint is public and RBAC-gated: an explicit decision.
resource "azurerm_container_registry" "session" {
  name                          = local.registry_name
  resource_group_name           = var.session_resource_group_name
  location                      = var.location
  sku                           = "Basic"
  admin_enabled                 = false
  public_network_access_enabled = true
  anonymous_pull_enabled        = false

  tags = local.session_tags
}

# --- Least-privilege bindings ----------------------------------------------------------

resource "azurerm_role_assignment" "kubelet_acr_pull" {
  scope                = azurerm_container_registry.session.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.lab.kubelet_identity[0].object_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "operator_acr_push" {
  scope                = azurerm_container_registry.session.id
  role_definition_name = "AcrPush"
  principal_id         = var.cluster_admin_principal_id
}

resource "azurerm_role_assignment" "operator_cluster_admin" {
  scope                = azurerm_kubernetes_cluster.lab.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = var.cluster_admin_principal_id
}

# Training Pods stage immutable inputs from the retained account and the
# export step writes model packages back to it (G4). Read here; the write
# path uses a workload-scoped grant G4 adds after the export design is fixed.
resource "azurerm_role_assignment" "kubelet_retained_reader" {
  scope                = var.retained_storage_account_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_kubernetes_cluster.lab.kubelet_identity[0].object_id
  principal_type       = "ServicePrincipal"
}
