terraform {
  required_version = ">= 1.11"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.40"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.4"
    }
    azapi = {
      source  = "azure/azapi"
      version = "~> 2.4"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # State goes in Guardrails' state storage, next to the platform's (see README).
  backend "azurerm" {
    key              = "loanlens.tfstate"
    use_azuread_auth = true
  }
}

provider "azurerm" {
  features {}
  storage_use_azuread = true # the storage account has no keys to use
}

provider "azapi" {}

# The credit union's own staff directory (underwriters sign in here, milestone 3).
provider "azuread" {}

# The separate Microsoft Entra External ID tenant where applicants have their accounts.
provider "azuread" {
  alias     = "customers"
  tenant_id = var.customer_tenant_id
}
