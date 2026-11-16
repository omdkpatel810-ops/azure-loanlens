# Milestone 3: the underwriter dashboard.
#
#   underwriter ──sign in (staff directory, Underwriter role)──▶ dashboard (Container Apps)
#        reads documents and readings, writes reviews, decisions and the audit trail;
#        asks Azure OpenAI (Canada East) for a summary of the figures, never the documents.
#
# Staff sign in with the credit union's own directory. Only people given the Underwriter app role get
# past the sign-in: assignment is required on the app, and the dashboard checks the role again.

# --- Who may use it -----------------------------------------------------------------------------------

resource "random_uuid" "underwriter_role" {}

resource "azuread_application" "dashboard" {
  display_name     = "LoanLens underwriter dashboard"
  sign_in_audience = "AzureADMyOrg"

  app_role {
    allowed_member_types = ["User"]
    description          = "Reviews loan applications' documents in LoanLens."
    display_name         = "Underwriter"
    id                   = random_uuid.underwriter_role.result
    value                = "Underwriter"
    enabled              = true
  }

  web {
    implicit_grant {
      id_token_issuance_enabled = true
    }
  }

  lifecycle {
    ignore_changes = [web[0].redirect_uris] # set below, once the dashboard's address exists
  }
}

resource "azuread_service_principal" "dashboard" {
  client_id                    = azuread_application.dashboard.client_id
  app_role_assignment_required = true # nobody without the role can even sign in
}

# People, not a group: assigning groups to apps needs a paid Entra ID plan.
data "azuread_user" "underwriter" {
  for_each            = toset(var.underwriters)
  user_principal_name = each.key
}

resource "azuread_app_role_assignment" "underwriter" {
  for_each            = data.azuread_user.underwriter
  app_role_id         = random_uuid.underwriter_role.result
  principal_object_id = each.value.object_id
  resource_object_id  = azuread_service_principal.dashboard.object_id
}

resource "azuread_application_redirect_uris" "dashboard" {
  application_id = azuread_application.dashboard.id
  type           = "Web"
  redirect_uris  = ["https://${azurerm_container_app.dashboard.ingress[0].fqdn}/.auth/login/aad/callback"]
}

resource "azuread_application_password" "dashboard" {
  application_id      = azuread_application.dashboard.id
  display_name        = "Container Apps sign-in"
  end_date            = timeadd(time_rotating.portal_secret.rfc3339, "2880h")
  rotate_when_changed = { rotation = time_rotating.portal_secret.id }
}

# --- Summaries: Azure OpenAI ---------------------------------------------------------------------------

resource "azurerm_cognitive_account" "openai" {
  #checkov:skip=CKV2_AZURE_22:Microsoft-managed keys; customer-managed keys would add a Key Vault and its upkeep.
  #checkov:skip=CKV_AZURE_247:Reached over the apps subnet's service endpoint and from the operator's address; network_acls deny the rest.
  #checkov:skip=CKV_AZURE_134:Same reason: public access stays on for the service endpoint, and network_acls deny the rest.
  name                          = "oai-loanlens-${local.suffix}"
  resource_group_name           = local.rg
  location                      = var.openai_location
  kind                          = "OpenAI"
  sku_name                      = "S0"
  custom_subdomain_name         = "oai-loanlens-${local.suffix}"
  local_auth_enabled            = false # no API keys
  public_network_access_enabled = true  # but see network_acls
  tags                          = local.tags

  identity {
    type = "SystemAssigned"
  }

  network_acls {
    default_action = "Deny"
    ip_rules       = [for r in var.operator_ip_ranges : trimsuffix(r, "/32")]
    virtual_network_rules {
      subnet_id = azurerm_subnet.apps.id
    }
  }
}

resource "azurerm_cognitive_deployment" "summaries" {
  name                 = "summaries"
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }

  sku {
    name     = "Standard" # processed in the account's region, so the figures stay in Canada
    capacity = var.openai_capacity
  }
}

# --- The dashboard -------------------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "dashboard" {
  name                = "id-loanlens-dashboard"
  resource_group_name = local.rg
  location            = local.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "dashboard" {
  for_each = merge(
    {
      view_uploads = { scope = azurerm_storage_container.uploads.id, role = "Storage Blob Data Reader" }
      readings     = { scope = azurerm_storage_table.records["readings"].id, role = "Storage Table Data Reader" }
      summaries_ai = { scope = azurerm_cognitive_account.openai.id, role = "Cognitive Services OpenAI User" }
      images       = { scope = azurerm_container_registry.images.id, role = "AcrPull" }
    },
    { for t in ["applications", "documents", "reviews", "audit", "summaries"] : t => {
      scope = azurerm_storage_table.records[t].id, role = "Storage Table Data Contributor"
    } }
  )

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.dashboard.principal_id
}

resource "azurerm_container_app" "dashboard" {
  name                         = "ca-loanlens-dashboard"
  resource_group_name          = local.rg
  container_app_environment_id = azurerm_container_app_environment.apps.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.dashboard.id]
  }

  registry {
    server   = azurerm_container_registry.images.login_server
    identity = azurerm_user_assigned_identity.dashboard.id
  }

  secret {
    name  = "appinsights-connection" # not a key: sending still needs the app's Entra ID token
    value = azurerm_application_insights.loanlens.connection_string
  }

  secret {
    name  = "microsoft-provider-authentication-secret"
    value = azuread_application_password.dashboard.value
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

    # Optionally, only from the credit union's offices.
    dynamic "ip_security_restriction" {
      for_each = var.staff_ip_ranges
      content {
        name             = "office-${ip_security_restriction.key}"
        action           = "Allow"
        ip_address_range = ip_security_restriction.value
      }
    }
  }

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name    = "dashboard"
      image   = local.placeholder ? "mcr.microsoft.com/k8se/quickstart:latest" : "${azurerm_container_registry.images.login_server}/loanlens:${var.image_tag}"
      command = local.placeholder ? null : ["uvicorn", "--factory", "loanlens.dashboard:from_env", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
      cpu     = 0.25
      memory  = "0.5Gi"

      env {
        name  = "LOANLENS_AUTH"
        value = "easyauth"
      }
      env {
        name  = "LOANLENS_STORAGE_ACCOUNT"
        value = azurerm_storage_account.documents.name
      }
      env {
        name  = "LOANLENS_OPENAI_ENDPOINT"
        value = azurerm_cognitive_account.openai.endpoint
      }
      env {
        name  = "LOANLENS_OPENAI_DEPLOYMENT"
        value = azurerm_cognitive_deployment.summaries.name
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-connection"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.dashboard.client_id
      }
    }
  }

  depends_on = [azurerm_role_assignment.dashboard]
}

resource "azapi_resource" "dashboard_sign_in" {
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.dashboard.id

  body = {
    properties = {
      platform = { enabled = true }
      globalValidation = {
        unauthenticatedClientAction = "RedirectToLoginPage"
        redirectToProvider          = "azureactivedirectory"
        excludedPaths               = ["/healthz"]
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId                = azuread_application.dashboard.client_id
            clientSecretSettingName = "microsoft-provider-authentication-secret"
            openIdIssuer            = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
          }
        }
      }
      httpSettings = { requireHttps = true }
    }
  }
}
