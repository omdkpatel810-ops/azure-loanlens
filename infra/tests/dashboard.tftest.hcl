# Offline tests: every provider is mocked, so these need no Azure account.

mock_provider "azurerm" {
  source = "./tests/mocks/azurerm"
}

mock_provider "azuread" {
  source = "./tests/mocks/azuread"
}

mock_provider "azuread" {
  alias  = "customers"
  source = "./tests/mocks/azuread"
}

mock_provider "azapi" {}

mock_provider "time" {
  mock_resource "time_rotating" {
    defaults = {
      id      = "2026-12-01T00:00:00Z"
      rfc3339 = "2026-12-01T00:00:00Z"
    }
  }
}

variables {
  operator_ip_ranges        = ["203.0.113.7/32"]
  customer_tenant_id        = "11111111-1111-1111-1111-111111111111"
  customer_tenant_subdomain = "loanlensapplicants"
}

run "only_assigned_underwriters_sign_in" {
  command = apply

  variables {
    underwriters = ["samira@example.com", "lee@example.com"]
  }

  assert {
    condition     = one(azuread_application.dashboard.app_role).value == "Underwriter" && azuread_service_principal.dashboard.app_role_assignment_required
    error_message = "One app role, Underwriter, and nobody without it can sign in."
  }

  assert {
    condition     = length(azuread_app_role_assignment.underwriter) == 2 && alltrue([for a in values(azuread_app_role_assignment.underwriter) : a.app_role_id == random_uuid.underwriter_role.result])
    error_message = "Each listed underwriter gets the role."
  }

  assert {
    condition     = azapi_resource.dashboard_sign_in.body.properties.identityProviders.azureActiveDirectory.registration.openIdIssuer == "https://login.microsoftonline.com/00000000-0000-0000-0000-00000000aaaa/v2.0"
    error_message = "Staff sign in with the credit union's own directory, not the applicants' tenant."
  }

  assert {
    condition     = azapi_resource.dashboard_sign_in.body.properties.globalValidation.unauthenticatedClientAction == "RedirectToLoginPage"
    error_message = "Every page but the health check needs a signed-in underwriter."
  }
}

run "the_dashboard_has_least_privilege" {
  command = apply

  assert {
    condition = { for k, r in azurerm_role_assignment.dashboard : k => r.role_definition_name } == {
      view_uploads = "Storage Blob Data Reader"
      readings     = "Storage Table Data Reader"
      summaries_ai = "Cognitive Services OpenAI User"
      images       = "AcrPull"
      applications = "Storage Table Data Contributor"
      documents    = "Storage Table Data Contributor"
      reviews      = "Storage Table Data Contributor"
      audit        = "Storage Table Data Contributor"
      summaries    = "Storage Table Data Contributor"
    }
    error_message = "View documents (never change them), read what the worker read, record reviews and decisions, ask for summaries."
  }
}

run "summaries_stay_in_canada_without_keys" {
  command = plan

  assert {
    condition     = azurerm_cognitive_account.openai.location == "canadaeast" && azurerm_cognitive_deployment.summaries.sku[0].name == "Standard"
    error_message = "A regional (Standard) deployment in Canada East, so the figures are processed in Canada."
  }

  assert {
    condition     = azurerm_cognitive_account.openai.local_auth_enabled == false && azurerm_cognitive_account.openai.network_acls[0].default_action == "Deny"
    error_message = "No API keys, and a closed network."
  }

  assert {
    condition     = azurerm_cognitive_deployment.summaries.model[0].name == "gpt-4o-mini"
    error_message = "A small, cheap model is plenty for three sentences."
  }
}

run "the_dashboard_can_be_limited_to_the_offices" {
  command = plan

  variables {
    staff_ip_ranges = ["198.51.100.0/24"]
    image_tag       = "4c1e956"
  }

  assert {
    condition     = one(azurerm_container_app.dashboard.ingress[0].ip_security_restriction).ip_address_range == "198.51.100.0/24"
    error_message = "Only the listed addresses reach the dashboard."
  }

  assert {
    condition     = contains(azurerm_container_app.dashboard.template[0].container[0].command, "loanlens.dashboard:from_env")
    error_message = "The same image, running the dashboard."
  }
}

run "refuses_openai_outside_canada" {
  command = plan

  variables {
    openai_location = "eastus"
  }

  expect_failures = [var.openai_location]
}

run "refuses_a_bad_underwriter_name" {
  command = plan

  variables {
    underwriters = ["Samira"]
  }

  expect_failures = [var.underwriters]
}
