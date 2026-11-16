# Milestone 2: the worker reads each document with Document Intelligence.
#
#   uploads queue ──▶ worker (Container Apps, scales with the queue) ──▶ Document Intelligence (custom T4 model)
#                        │ reads the blob, writes what it found to the readings table
#                        └─ failures that won't fix themselves ──▶ uploads-poison queue
#
# Document Intelligence has no keys either: the worker signs in with its identity, and the service
# reads the training folder with its own. Its network is closed like storage's.

resource "azurerm_cognitive_account" "documents" {
  #checkov:skip=CKV2_AZURE_22:Microsoft-managed keys; customer-managed keys would add a Key Vault and its upkeep.
  #checkov:skip=CKV_AZURE_247:The worker reaches it over a service endpoint and the operator from one address; network_acls refuse everything else.
  #checkov:skip=CKV_AZURE_134:Same reason: public access stays on for the service endpoint and the operator, and network_acls deny the rest.
  name                          = "di-loanlens-${local.suffix}"
  resource_group_name           = local.rg
  location                      = local.location
  kind                          = "FormRecognizer" # Azure AI Document Intelligence
  sku_name                      = var.document_intelligence_sku
  custom_subdomain_name         = "di-loanlens-${local.suffix}" # needed for Entra ID sign-in
  local_auth_enabled            = false                         # no API keys
  public_network_access_enabled = true                          # but see network_acls
  tags                          = local.tags

  identity {
    type = "SystemAssigned" # to read the training folder
  }

  network_acls {
    default_action = "Deny"
    ip_rules       = [for r in var.operator_ip_ranges : trimsuffix(r, "/32")]
    virtual_network_rules {
      subnet_id = azurerm_subnet.apps.id
    }
  }
}

# tflint-ignore: azurerm_resources_missing_prevent_destroy # generated samples; `python -m loanlens.samples` makes them again
resource "azurerm_storage_container" "training" {
  #checkov:skip=CKV2_AZURE_21:Only generated sample slips live here.
  name                  = "training"
  storage_account_id    = azurerm_storage_account.documents.id
  container_access_type = "private"
}

resource "azurerm_role_assignment" "model_reads_training" {
  scope                = azurerm_storage_container.training.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_cognitive_account.documents.identity[0].principal_id
}

# The operator trains the model: upload the samples, call Document Intelligence.
resource "azurerm_role_assignment" "operator" {
  for_each = {
    training = { scope = azurerm_storage_container.training.id, role = "Storage Blob Data Contributor" }
    model    = { scope = azurerm_cognitive_account.documents.id, role = "Cognitive Services User" }
  }

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_user_assigned_identity" "worker" {
  name                = "id-loanlens-worker"
  resource_group_name = local.rg
  location            = local.location
  tags                = local.tags
}

# Read documents, take and delete messages, set aside poison ones, record readings, call the model.
resource "azurerm_role_assignment" "worker" {
  for_each = {
    read_uploads = { scope = azurerm_storage_container.uploads.id, role = "Storage Blob Data Reader" }
    take_events  = { scope = azurerm_storage_queue.uploads["uploads"].id, role = "Storage Queue Data Message Processor" }
    set_aside    = { scope = azurerm_storage_queue.uploads["uploads-poison"].id, role = "Storage Queue Data Message Sender" }
    documents    = { scope = azurerm_storage_table.records["documents"].id, role = "Storage Table Data Contributor" }
    readings     = { scope = azurerm_storage_table.records["readings"].id, role = "Storage Table Data Contributor" }
    model        = { scope = azurerm_cognitive_account.documents.id, role = "Cognitive Services User" }
    images       = { scope = azurerm_container_registry.images.id, role = "AcrPull" }
  }

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.worker.principal_id
}

resource "azurerm_container_app" "worker" {
  name                         = "ca-loanlens-worker"
  resource_group_name          = local.rg
  container_app_environment_id = azurerm_container_app_environment.apps.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.worker.id]
  }

  registry {
    server   = azurerm_container_registry.images.login_server
    identity = azurerm_user_assigned_identity.worker.id
  }

  # No ingress: nothing can call the worker. It only takes work from the queue.

  template {
    min_replicas = 0 # an empty queue, nothing running
    max_replicas = 5

    custom_scale_rule {
      name             = "uploads-queue"
      custom_rule_type = "azure-queue"
      identity_id      = azurerm_user_assigned_identity.worker.id # KEDA reads the queue length as the worker
      metadata = {
        accountName = azurerm_storage_account.documents.name
        queueName   = "uploads"
        queueLength = "5" # one more replica for every 5 waiting documents
      }
    }

    container {
      name    = "worker"
      image   = local.placeholder ? "mcr.microsoft.com/k8se/quickstart:latest" : "${azurerm_container_registry.images.login_server}/loanlens:${var.image_tag}"
      command = local.placeholder ? null : ["python", "-m", "loanlens.worker"]
      cpu     = 0.5
      memory  = "1Gi"

      env {
        name  = "LOANLENS_STORAGE_ACCOUNT"
        value = azurerm_storage_account.documents.name
      }
      env {
        name  = "LOANLENS_DI_ENDPOINT"
        value = azurerm_cognitive_account.documents.endpoint
      }
      env {
        name  = "LOANLENS_DI_MODEL"
        value = var.document_model_id
      }
      env {
        name  = "LOANLENS_CONFIDENCE"
        value = tostring(var.confidence_threshold)
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.worker.client_id
      }
    }
  }

  depends_on = [azurerm_role_assignment.worker]
}
