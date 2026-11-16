# --- The portal on Azure Container Apps -------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "apps" {
  name                = "log-loanlens-${local.suffix}"
  resource_group_name = local.rg
  location            = local.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.2
  tags                = local.tags
}

resource "azurerm_container_registry" "images" {
  #checkov:skip=CKV_AZURE_139:GitHub Actions pushes images over the internet; pushing needs an Entra ID token with AcrPush.
  #checkov:skip=CKV_AZURE_163:Image scanning is a Defender for Containers feature; the CI pipeline scans images instead.
  #checkov:skip=CKV_AZURE_164:Content trust needs the Premium tier.
  #checkov:skip=CKV_AZURE_165:Geo-replication needs the Premium tier; milestone 4's failover rebuilds from GitHub.
  #checkov:skip=CKV_AZURE_166:Quarantine needs the Premium tier.
  #checkov:skip=CKV_AZURE_167:Retention for untagged images needs the Premium tier.
  #checkov:skip=CKV_AZURE_233:Zone redundancy needs the Premium tier.
  #checkov:skip=CKV_AZURE_237:Dedicated data endpoints need the Premium tier.
  name                = "crloanlens${local.suffix}"
  resource_group_name = local.rg
  location            = local.location
  sku                 = "Basic" # about $5 a month
  admin_enabled       = false   # no shared username and password; pulls use the portal's identity
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "portal" {
  name                = "id-loanlens-portal"
  resource_group_name = local.rg
  location            = local.location
  tags                = local.tags
}

# Exactly what the portal needs: write documents, keep its two tables, pull its image. It can't read
# or change the queues, and nothing else in the subscription.
resource "azurerm_role_assignment" "portal" {
  for_each = {
    uploads      = { scope = azurerm_storage_container.uploads.id, role = "Storage Blob Data Contributor" }
    applications = { scope = azurerm_storage_table.records["applications"].id, role = "Storage Table Data Contributor" }
    documents    = { scope = azurerm_storage_table.records["documents"].id, role = "Storage Table Data Contributor" }
    images       = { scope = azurerm_container_registry.images.id, role = "AcrPull" }
  }

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.portal.principal_id
}

resource "azurerm_container_app_environment" "apps" {
  name                       = "cae-loanlens"
  resource_group_name        = local.rg
  location                   = local.location
  infrastructure_subnet_id   = azurerm_subnet.apps.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.apps.id
  tags                       = local.tags

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption" # pay per use, scales to zero
  }
}

locals {
  placeholder = var.image_tag == ""
  portal_port = local.placeholder ? 80 : 8000
}

resource "azurerm_container_app" "portal" {
  name                         = "ca-loanlens-portal"
  resource_group_name          = local.rg
  container_app_environment_id = azurerm_container_app_environment.apps.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.portal.id]
  }

  registry {
    server   = azurerm_container_registry.images.login_server
    identity = azurerm_user_assigned_identity.portal.id
  }

  secret {
    name  = "appinsights-connection" # not a key: sending still needs the app's Entra ID token
    value = azurerm_application_insights.loanlens.connection_string
  }

  secret {
    name  = "microsoft-provider-authentication-secret"
    value = azuread_application_password.portal.value
  }

  ingress {
    external_enabled           = true
    target_port                = local.portal_port
    allow_insecure_connections = false
    transport                  = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0 # nobody uploading, nothing running
    max_replicas = 3

    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }

    container {
      name   = "portal"
      image  = local.placeholder ? "mcr.microsoft.com/k8se/quickstart:latest" : "${azurerm_container_registry.images.login_server}/loanlens:${var.image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "LOANLENS_AUTH"
        value = "easyauth"
      }
      env {
        name  = "LOANLENS_STORAGE_ACCOUNT"
        value = azurerm_storage_account.documents.name
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-connection"
      }
      env {
        name  = "AZURE_CLIENT_ID" # which identity DefaultAzureCredential uses
        value = azurerm_user_assigned_identity.portal.client_id
      }

      dynamic "liveness_probe" {
        for_each = local.placeholder ? [] : [1]
        content {
          transport = "HTTP"
          path      = "/healthz"
          port      = local.portal_port
        }
      }
    }
  }

  depends_on = [azurerm_role_assignment.portal]
}
