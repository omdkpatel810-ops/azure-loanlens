# Milestone 1: the secure upload portal.
#
#   applicant ──sign in (External ID)──▶ portal (Container Apps) ──managed identity──▶ private storage
#                                                                                        │ BlobCreated
#                                                                    uploads queue ◀── Event Grid
#
# The storage account has no keys and refuses every network but LoanLens' own (and the operator's,
# for Terraform). The portal reaches it over a service endpoint, signed in as its own identity, with
# rights to the uploads container and two tables and nothing else.

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "loanlens" {
  name = var.resource_group_name
}

locals {
  suffix   = substr(sha1("${data.azurerm_client_config.current.subscription_id}-loanlens"), 0, 8)
  location = data.azurerm_resource_group.loanlens.location
  rg       = data.azurerm_resource_group.loanlens.name
  # Same tags as the resource group, so Guardrails' tag inheritance never shows up as drift.
  tags = { for k in ["owner", "cost-center", "environment", "team"] : k => data.azurerm_resource_group.loanlens.tags[k] if contains(keys(data.azurerm_resource_group.loanlens.tags), k) }
}

# --- Network ----------------------------------------------------------------------------------------

resource "azurerm_virtual_network" "loanlens" {
  name                = "vnet-loanlens"
  resource_group_name = local.rg
  location            = local.location
  address_space       = [var.address_space]
  tags                = local.tags
}

resource "azurerm_subnet" "apps" {
  name                 = "snet-apps"
  resource_group_name  = local.rg
  virtual_network_name = azurerm_virtual_network.loanlens.name
  address_prefixes     = [cidrsubnet(var.address_space, 27 - tonumber(split("/", var.address_space)[1]), 0)]
  service_endpoints    = ["Microsoft.Storage", "Microsoft.CognitiveServices"]

  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_network_security_group" "apps" {
  name                = "nsg-apps"
  resource_group_name = local.rg
  location            = local.location
  tags                = local.tags
}

resource "azurerm_subnet_network_security_group_association" "apps" {
  subnet_id                 = azurerm_subnet.apps.id
  network_security_group_id = azurerm_network_security_group.apps.id
}
