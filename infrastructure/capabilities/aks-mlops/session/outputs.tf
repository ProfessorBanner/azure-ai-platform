output "cluster_id" {
  description = "Resource ID of the session AKS cluster (the watchdog's primary deletion target)."
  value       = azurerm_kubernetes_cluster.lab.id
}

output "cluster_name" {
  description = "AKS cluster name."
  value       = azurerm_kubernetes_cluster.lab.name
}

output "node_resource_group" {
  description = "AKS-managed node resource group; in the watchdog inventory and the budget filter."
  value       = azurerm_kubernetes_cluster.lab.node_resource_group
}

output "kubelet_identity_object_id" {
  description = "Object ID of the kubelet managed identity (AcrPull, retained Storage Blob Data Reader)."
  value       = azurerm_kubernetes_cluster.lab.kubelet_identity[0].object_id
}

output "container_registry_login_server" {
  description = "Login server of the session registry (push target for the G4 amd64 images)."
  value       = azurerm_container_registry.session.login_server
}

output "gpu_node_pool_enabled" {
  description = "Whether the GPU pool exists in this plan/apply."
  value       = var.gpu_node_pool_enabled
}

output "session" {
  description = "The identity this root stamped on every resource; must match the ledger reservation and the armed controls."
  value = {
    session_id = var.session_id
    expiry_utc = var.expiry_utc
  }
}

output "access_commands" {
  description = "How to reach the lab: Entra login for kubectl, then port-forward. No public ingress exists."
  value = {
    credentials  = "az aks get-credentials --subscription ${var.subscription_id} --resource-group ${var.session_resource_group_name} --name ${azurerm_kubernetes_cluster.lab.name} --overwrite-existing"
    kubelogin    = "kubelogin convert-kubeconfig -l azurecli"
    port_forward = "kubectl --context ${azurerm_kubernetes_cluster.lab.name} -n pet-g2 port-forward svc/pet-g2-api 8000:8000"
  }
}
