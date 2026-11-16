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

run "document_intelligence_has_no_keys_and_no_open_network" {
  command = plan

  assert {
    condition     = azurerm_cognitive_account.documents.kind == "FormRecognizer" && azurerm_cognitive_account.documents.local_auth_enabled == false
    error_message = "Document Intelligence, signed in to with Entra ID only."
  }

  assert {
    condition     = azurerm_cognitive_account.documents.network_acls[0].default_action == "Deny" && azurerm_cognitive_account.documents.network_acls[0].ip_rules == toset(["203.0.113.7"])
    error_message = "Every network refused but LoanLens' own and the operator's."
  }

  assert {
    condition     = contains(azurerm_subnet.apps.service_endpoints, "Microsoft.CognitiveServices")
    error_message = "The worker reaches it over a service endpoint."
  }
}

run "the_worker_has_least_privilege" {
  command = apply

  assert {
    condition = { for k, r in azurerm_role_assignment.worker : k => r.role_definition_name } == {
      read_uploads = "Storage Blob Data Reader"
      take_events  = "Storage Queue Data Message Processor"
      set_aside    = "Storage Queue Data Message Sender"
      documents    = "Storage Table Data Contributor"
      readings     = "Storage Table Data Contributor"
      model        = "Cognitive Services User"
      images       = "AcrPull"
    }
    error_message = "Read documents (never write them), take messages, set aside poison ones, record readings, call the model."
  }

  assert {
    condition     = endswith(azurerm_role_assignment.worker["read_uploads"].scope, "/containers/uploads") && azurerm_role_assignment.worker["model"].scope == azurerm_cognitive_account.documents.id
    error_message = "Scoped to the uploads container and the one Document Intelligence account."
  }

  assert {
    condition     = azurerm_role_assignment.model_reads_training.role_definition_name == "Storage Blob Data Reader" && azurerm_role_assignment.model_reads_training.scope == azurerm_storage_container.training.id
    error_message = "Document Intelligence can read the training folder, and only that."
  }

  assert {
    condition     = one(azurerm_storage_account.documents.network_rules[0].private_link_access).endpoint_resource_id == azurerm_cognitive_account.documents.id
    error_message = "The storage firewall lets Document Intelligence's identity through, and no other service."
  }
}

run "the_worker_scales_with_the_queue_and_takes_no_calls" {
  command = plan

  variables {
    image_tag = "4c1e956"
  }

  assert {
    condition     = length(azurerm_container_app.worker.ingress) == 0 && azurerm_container_app.worker.template[0].min_replicas == 0
    error_message = "No way in, and nothing running when the queue is empty."
  }

  assert {
    condition     = azurerm_container_app.worker.template[0].custom_scale_rule[0].custom_rule_type == "azure-queue" && azurerm_container_app.worker.template[0].custom_scale_rule[0].metadata.queueName == "uploads"
    error_message = "Scales on the uploads queue's length."
  }

  assert {
    condition     = azurerm_container_app.worker.template[0].container[0].command == tolist(["python", "-m", "loanlens.worker"])
    error_message = "The same image as the portal, running the worker."
  }

  assert {
    condition     = { for e in azurerm_container_app.worker.template[0].container[0].env : e.name => e.value }["LOANLENS_DI_MODEL"] == "loanlens-t4-v1"
    error_message = "Uses the custom T4 model."
  }
}

run "refuses_an_unknown_tier" {
  command = plan

  variables {
    document_intelligence_sku = "S1"
  }

  expect_failures = [var.document_intelligence_sku]
}

run "refuses_a_threshold_that_trusts_everything" {
  command = plan

  variables {
    confidence_threshold = 0.2
  }

  expect_failures = [var.confidence_threshold]
}
