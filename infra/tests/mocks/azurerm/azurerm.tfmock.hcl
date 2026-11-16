# Shared stand-ins for Azure, used by every test file.

mock_data "azurerm_client_config" {
  defaults = {
    tenant_id       = "00000000-0000-0000-0000-00000000aaaa"
    subscription_id = "00000000-0000-0000-0000-00000000bbbb"
    object_id       = "00000000-0000-0000-0000-00000000cccc"
  }
}

mock_data "azurerm_resource_group" {
  defaults = {
    id       = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod"
    location = "canadacentral"
    tags     = { owner = "lending-lead@example.com", "cost-center" = "cc-7400", environment = "prod", team = "loanlens", request = "loanlens-prod.json" }
  }
}

mock_resource "azurerm_virtual_network" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Network/virtualNetworks/vnet-loanlens" }
}

mock_resource "azurerm_subnet" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Network/virtualNetworks/vnet-loanlens/subnets/snet-apps" }
}

mock_resource "azurerm_network_security_group" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Network/networkSecurityGroups/nsg-apps" }
}

mock_resource "azurerm_storage_account" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Storage/storageAccounts/stloanlens" }
}

mock_resource "azurerm_storage_container" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Storage/storageAccounts/stloanlens/blobServices/default/containers/uploads" }
}

mock_resource "azurerm_storage_queue" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Storage/storageAccounts/stloanlens/queueServices/default/queues/uploads" }
}

mock_resource "azurerm_storage_table" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.Storage/storageAccounts/stloanlens/tableServices/default/tables/applications" }
}

mock_resource "azurerm_eventgrid_system_topic" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.EventGrid/systemTopics/evgt-loanlens-documents" }
}

mock_resource "azurerm_log_analytics_workspace" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.OperationalInsights/workspaces/log-loanlens" }
}

mock_resource "azurerm_container_registry" {
  defaults = {
    id           = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.ContainerRegistry/registries/crloanlens"
    login_server = "crloanlens.azurecr.io"
  }
}

mock_resource "azurerm_user_assigned_identity" {
  defaults = {
    id           = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-loanlens-portal"
    principal_id = "00000000-0000-0000-0000-0000000d0d0d"
    client_id    = "00000000-0000-0000-0000-0000000c1c1c"
  }
}

mock_resource "azurerm_container_app_environment" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.App/managedEnvironments/cae-loanlens" }
}

mock_resource "azurerm_container_app" {
  defaults = { id = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.App/containerApps/ca-loanlens-portal" }
}

mock_resource "azurerm_cognitive_account" {
  defaults = {
    id       = "/subscriptions/00000000-0000-0000-0000-00000000bbbb/resourceGroups/rg-loanlens-prod/providers/Microsoft.CognitiveServices/accounts/di-loanlens"
    endpoint = "https://di-loanlens.cognitiveservices.azure.com/"
  }
}
