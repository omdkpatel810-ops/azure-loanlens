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

run "storage_has_no_keys_and_no_open_network" {
  command = plan

  assert {
    condition     = azurerm_storage_account.documents.shared_access_key_enabled == false && azurerm_storage_account.documents.default_to_oauth_authentication
    error_message = "No account keys: everything signs in with Entra ID."
  }

  assert {
    condition     = azurerm_storage_account.documents.network_rules[0].default_action == "Deny" && azurerm_storage_account.documents.network_rules[0].ip_rules == toset(["203.0.113.7"])
    error_message = "Every network refused but LoanLens' own and the operator's address."
  }

  assert {
    condition     = azurerm_storage_account.documents.allow_nested_items_to_be_public == false && azurerm_storage_container.uploads.container_access_type == "private"
    error_message = "No public blobs, ever."
  }

  assert {
    condition     = azurerm_storage_account.documents.min_tls_version == "TLS1_2" && azurerm_storage_account.documents.infrastructure_encryption_enabled
    error_message = "TLS 1.2 and double encryption."
  }

  assert {
    condition     = azurerm_storage_account.documents.account_replication_type == "RAGRS" && azurerm_storage_account.documents.blob_properties[0].versioning_enabled && azurerm_storage_account.documents.blob_properties[0].delete_retention_policy[0].days == 30
    error_message = "A copy in Canada East, versions, and 30 days to undo a deletion."
  }
}

run "documents_kept_seven_years_then_deleted" {
  command = plan

  assert {
    condition     = azurerm_storage_management_policy.documents.rule[0].actions[0].base_blob[0].delete_after_days_since_modification_greater_than == 2555 && azurerm_storage_management_policy.documents.rule[0].actions[0].base_blob[0].tier_to_cool_after_days_since_modification_greater_than == 30
    error_message = "Cheaper storage after a month, gone after the retention period."
  }
}

run "every_upload_becomes_a_queue_message" {
  command = plan

  assert {
    condition     = azurerm_eventgrid_system_topic_event_subscription.uploads.included_event_types == tolist(["Microsoft.Storage.BlobCreated"]) && azurerm_eventgrid_system_topic_event_subscription.uploads.subject_filter[0].subject_begins_with == "/blobServices/default/containers/uploads/blobs/"
    error_message = "New blobs in the uploads container only."
  }

  assert {
    condition     = azurerm_eventgrid_system_topic_event_subscription.uploads.storage_queue_endpoint[0].queue_name == "uploads" && azurerm_eventgrid_system_topic_event_subscription.uploads.delivery_identity[0].type == "SystemAssigned"
    error_message = "Delivered to the uploads queue with Event Grid's own identity, since the account has no keys."
  }

  assert {
    condition     = azurerm_role_assignment.events_to_queue.role_definition_name == "Storage Queue Data Message Sender"
    error_message = "Event Grid can add messages to the queue and do nothing else."
  }
}

run "the_portal_has_least_privilege" {
  command = apply

  assert {
    condition     = toset([for r in values(azurerm_role_assignment.portal) : r.role_definition_name]) == toset(["Storage Blob Data Contributor", "Storage Table Data Contributor", "AcrPull"])
    error_message = "Write documents, keep its tables, pull its image."
  }

  assert {
    condition     = endswith(azurerm_role_assignment.portal["uploads"].scope, "/containers/uploads") && endswith(azurerm_role_assignment.portal["applications"].scope, "/tables/applications")
    error_message = "Scoped to the one container and the two tables, not the whole account."
  }

  assert {
    condition     = azurerm_container_registry.images.admin_enabled == false && azurerm_container_app.portal.registry[0].identity == azurerm_user_assigned_identity.portal.id
    error_message = "Images are pulled with the portal's identity, not a shared password."
  }
}

run "the_portal_scales_to_zero_behind_https_sign_in" {
  command = plan

  assert {
    condition     = azurerm_container_app.portal.template[0].min_replicas == 0 && azurerm_container_app.portal.ingress[0].allow_insecure_connections == false
    error_message = "Nothing runs when nobody's uploading; HTTPS only."
  }

  assert {
    condition     = azapi_resource.portal_sign_in.body.properties.globalValidation.unauthenticatedClientAction == "RedirectToLoginPage" && azapi_resource.portal_sign_in.body.properties.globalValidation.excludedPaths == ["/healthz"]
    error_message = "Every page but the health check needs a signed-in applicant."
  }

  assert {
    condition     = azapi_resource.portal_sign_in.body.properties.identityProviders.azureActiveDirectory.registration.openIdIssuer == "https://loanlensapplicants.ciamlogin.com/11111111-1111-1111-1111-111111111111/v2.0"
    error_message = "Applicants sign in with the External ID tenant, not the staff directory."
  }

  assert {
    condition     = { for e in azurerm_container_app.portal.template[0].container[0].env : e.name => e.value }["LOANLENS_AUTH"] == "easyauth"
    error_message = "The portal trusts only the platform's sign-in headers."
  }
}

run "first_apply_runs_a_placeholder" {
  command = plan

  assert {
    condition     = azurerm_container_app.portal.template[0].container[0].image == "mcr.microsoft.com/k8se/quickstart:latest" && azurerm_container_app.portal.ingress[0].target_port == 80
    error_message = "Before CI has pushed an image, a placeholder keeps the first apply working."
  }
}

run "then_the_real_image" {
  command = plan

  variables {
    image_tag = "4c1e956"
  }

  assert {
    condition     = azurerm_container_app.portal.template[0].container[0].image == "crloanlens.azurecr.io/loanlens:4c1e956" && azurerm_container_app.portal.ingress[0].target_port == 8000
    error_message = "The portal image from LoanLens' registry, on port 8000."
  }

  assert {
    condition     = azurerm_container_app.portal.template[0].container[0].liveness_probe[0].path == "/healthz"
    error_message = "Restarted if the health check fails."
  }
}

run "the_sign_in_secret_rotates_with_overlap" {
  command = apply

  assert {
    condition     = azuread_application_password.portal.end_date == "2027-03-31T00:00:00Z"
    error_message = "Each secret lasts 120 days and is replaced after 90."
  }

  assert {
    condition     = azuread_application_redirect_uris.portal.redirect_uris == toset(["https://${azurerm_container_app.portal.ingress[0].fqdn}/.auth/login/aad/callback"])
    error_message = "Sign-in returns to the portal's own address."
  }
}

run "refuses_an_open_operator_range" {
  command = plan

  variables {
    operator_ip_ranges = ["0.0.0.0/0"]
  }

  expect_failures = [var.operator_ip_ranges]
}

run "refuses_a_network_outside_the_plan" {
  command = plan

  variables {
    address_space = "192.168.0.0/24"
  }

  expect_failures = [var.address_space]
}

run "refuses_a_bad_tenant" {
  command = plan

  variables {
    customer_tenant_id = "contoso"
  }

  expect_failures = [var.customer_tenant_id]
}
