# Azure Databricks workspace — reusable, environment-neutral module.
#
# Durable, Terraform-managed platform infrastructure. Scope is deliberately
# minimal: ONE workspace. No clusters, jobs, notebooks, serving endpoints or
# persistent compute are managed here, and the Databricks Terraform provider is
# NOT used — those (and Unity Catalog) belong to Phase 8. Workloads are deployed
# later via Databricks Declarative Automation Bundles, not Terraform.
#
# Posture (Phase 7):
#   - Premium SKU (required for Unity Catalog and workspace RBAC).
#   - Secure Cluster Connectivity (no_public_ip = true) on the Databricks-managed
#     VNet: compute nodes carry no public IPs. Set at creation because toggling
#     it later forces a workspace replacement.
#   - No VNet injection, no Private Link, no customer-managed keys. The control
#     plane keeps public network access enabled so CI agents and the operator can
#     reach it without a private endpoint — a documented interim posture, not the
#     intended production posture (see ADR 0005).
resource "azurerm_databricks_workspace" "this" {
  name                = var.databricks_workspace_name
  resource_group_name = var.resource_group_name
  location            = var.location

  sku                           = var.sku
  public_network_access_enabled = var.public_network_access_enabled

  custom_parameters {
    no_public_ip                                         = var.no_public_ip
    virtual_network_id                                   = var.virtual_network_id
    public_subnet_name                                   = var.public_subnet_name
    private_subnet_name                                  = var.private_subnet_name
    public_subnet_network_security_group_association_id  = var.public_subnet_network_security_group_association_id
    private_subnet_network_security_group_association_id = var.private_subnet_network_security_group_association_id
  }

  tags = var.tags

  lifecycle {
    # A workspace is durable governed infrastructure; never destroy or replace it
    # as a routine action. Removal, if ever truly required, is a deliberate
    # reviewed change, never `terraform destroy`. prevent_destroy is a literal
    # (Terraform disallows variables here), so it applies to every environment,
    # including sandbox — disposal there requires an explicit code change.
    prevent_destroy = true
  }
}
