# --- Applicant sign-in: Microsoft Entra External ID, through Container Apps' built-in sign-in -------
#
# Applicants have accounts in a separate External ID tenant (free for the first 50,000 people a month),
# never in the credit union's staff directory. Container Apps does the sign-in before a request reaches
# the portal; the portal only reads who's signed in from the headers the platform adds.
#
# Made by hand once, in the External ID tenant (see README): the tenant itself, and a sign-up and
# sign-in user flow with this app added to it.

resource "azuread_application" "portal" {
  provider         = azuread.customers
  display_name     = "LoanLens applicant portal"
  sign_in_audience = "AzureADMyOrg"

  web {
    implicit_grant {
      id_token_issuance_enabled = true # Container Apps' sign-in uses an ID token
    }
  }

  lifecycle {
    ignore_changes = [web[0].redirect_uris] # set below, once the portal's address exists
  }
}

resource "azuread_service_principal" "portal" {
  provider  = azuread.customers
  client_id = azuread_application.portal.client_id
}

resource "azuread_application_redirect_uris" "portal" {
  provider       = azuread.customers
  application_id = azuread_application.portal.id
  type           = "Web"
  redirect_uris  = ["https://${azurerm_container_app.portal.ingress[0].fqdn}/.auth/login/aad/callback"]
}

# The sign-in secret rotates every 90 days on the next apply, and each one lasts 120 days, so there's
# always a month's overlap. It goes straight from Entra ID into a Container Apps secret.
resource "time_rotating" "portal_secret" {
  rotation_days = 90
}

resource "azuread_application_password" "portal" {
  provider            = azuread.customers
  application_id      = azuread_application.portal.id
  display_name        = "Container Apps sign-in"
  end_date            = timeadd(time_rotating.portal_secret.rfc3339, "2880h")
  rotate_when_changed = { rotation = time_rotating.portal_secret.id }
}

resource "azapi_resource" "portal_sign_in" {
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.portal.id

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
            clientId                = azuread_application.portal.client_id
            clientSecretSettingName = "microsoft-provider-authentication-secret"
            openIdIssuer            = "https://${var.customer_tenant_subdomain}.ciamlogin.com/${var.customer_tenant_id}/v2.0"
          }
        }
      }
      httpSettings = { requireHttps = true }
    }
  }
}
