# --- Private document storage, and the upload events ------------------------------------------------

resource "azurerm_storage_account" "documents" {
  #checkov:skip=CKV_AZURE_59:Public network access stays on so the service endpoint and the operator's IP work; network_rules refuse everything else.
  #checkov:skip=CKV2_AZURE_33:A private endpoint costs about $7 a month each; the service endpoint keeps traffic on Azure's network for free.
  #checkov:skip=CKV2_AZURE_1:Microsoft-managed keys, plus infrastructure encryption; customer-managed keys would add a Key Vault and its upkeep.
  #checkov:skip=CKV_AZURE_33:Queue logging goes to Azure Monitor diagnostic settings (milestone 4), not the classic logs.
  #checkov:skip=CKV_AZURE_206:Read-access geo-redundant storage is the cheapest option with a copy in Canada East for the failover drill.
  #checkov:skip=CKV_AZURE_43:Checkov can't read the interpolated name; it's "stloanlens" and 8 hex characters, 18 lowercase letters and digits.
  name                              = "stloanlens${local.suffix}"
  resource_group_name               = local.rg
  location                          = local.location
  account_tier                      = "Standard"
  account_kind                      = "StorageV2"
  account_replication_type          = "RAGRS" # a second copy in Canada East, for milestone 4's failover drill
  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  shared_access_key_enabled         = false # no keys: Entra ID sign-in only
  default_to_oauth_authentication   = true
  allow_nested_items_to_be_public   = false
  cross_tenant_replication_enabled  = false
  infrastructure_encryption_enabled = true
  local_user_enabled                = false
  sftp_enabled                      = false
  public_network_access_enabled     = true # but see network_rules
  tags                              = local.tags

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"] # Event Grid, delivering with its managed identity
    virtual_network_subnet_ids = [azurerm_subnet.apps.id]
    ip_rules                   = [for r in var.operator_ip_ranges : trimsuffix(r, "/32")]

    # Document Intelligence, reading the training folder with its own identity (milestone 2).
    private_link_access {
      endpoint_resource_id = azurerm_cognitive_account.documents.id
      endpoint_tenant_id   = data.azurerm_client_config.current.tenant_id
    }
  }

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  sas_policy {
    expiration_period = "00.01:00:00"
  }

  lifecycle {
    prevent_destroy = true # applicants' documents
  }
}

resource "azurerm_storage_container" "uploads" {
  #checkov:skip=CKV2_AZURE_21:Blob read logging goes to Azure Monitor diagnostic settings (milestone 4).
  name                  = "uploads"
  storage_account_id    = azurerm_storage_account.documents.id
  container_access_type = "private"

  lifecycle {
    prevent_destroy = true
  }
}

# tflint-ignore: azurerm_resources_missing_prevent_destroy # only undeliverable events, safe to recreate
resource "azurerm_storage_container" "dead_letter" {
  #checkov:skip=CKV2_AZURE_21:Holds only undeliverable events; nothing reads it routinely.
  name                  = "events-dead-letter"
  storage_account_id    = azurerm_storage_account.documents.id
  container_access_type = "private"
}

resource "azurerm_storage_queue" "uploads" {
  for_each = toset(["uploads", "uploads-poison"])

  name               = each.key
  storage_account_id = azurerm_storage_account.documents.id

  lifecycle {
    prevent_destroy = true # documents waiting to be read
  }
}

resource "azurerm_storage_table" "records" {
  #checkov:skip=CKV2_AZURE_20:Table reads are logged through Azure Monitor diagnostic settings (milestone 4), not the classic logs.
  for_each = toset(["applications", "documents", "readings", "reviews", "audit", "summaries"])

  name               = each.key
  storage_account_id = azurerm_storage_account.documents.id

  lifecycle {
    prevent_destroy = true # applications and their documents' records
  }
}

resource "azurerm_storage_management_policy" "documents" {
  storage_account_id = azurerm_storage_account.documents.id

  rule {
    name    = "uploads"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["uploads/"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 30
        delete_after_days_since_modification_greater_than       = var.document_retention_days
      }
      version {
        delete_after_days_since_creation = 30
      }
    }
  }
}

# Every new upload becomes a message on the uploads queue, for milestone 2's worker.
resource "azurerm_eventgrid_system_topic" "documents" {
  name                = "evgt-loanlens-documents"
  resource_group_name = local.rg
  location            = local.location
  source_resource_id  = azurerm_storage_account.documents.id
  topic_type          = "Microsoft.Storage.StorageAccounts"
  tags                = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "events_to_queue" {
  scope                = azurerm_storage_queue.uploads["uploads"].id
  role_definition_name = "Storage Queue Data Message Sender"
  principal_id         = azurerm_eventgrid_system_topic.documents.identity[0].principal_id
}

resource "azurerm_role_assignment" "events_dead_letter" {
  scope                = azurerm_storage_container.dead_letter.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_eventgrid_system_topic.documents.identity[0].principal_id
}

resource "azurerm_eventgrid_system_topic_event_subscription" "uploads" {
  name                 = "uploads-to-queue"
  system_topic         = azurerm_eventgrid_system_topic.documents.name
  resource_group_name  = local.rg
  included_event_types = ["Microsoft.Storage.BlobCreated"]

  subject_filter {
    subject_begins_with = "/blobServices/default/containers/uploads/blobs/"
  }

  delivery_identity {
    type = "SystemAssigned"
  }

  storage_queue_endpoint {
    storage_account_id                    = azurerm_storage_account.documents.id
    queue_name                            = azurerm_storage_queue.uploads["uploads"].name
    queue_message_time_to_live_in_seconds = 604800 # a week: long enough to survive a worker outage
  }

  retry_policy {
    max_delivery_attempts = 30
    event_time_to_live    = 1440
  }

  dead_letter_identity {
    type = "SystemAssigned"
  }

  storage_blob_dead_letter_destination {
    storage_account_id          = azurerm_storage_account.documents.id
    storage_blob_container_name = azurerm_storage_container.dead_letter.name
  }

  depends_on = [azurerm_role_assignment.events_to_queue, azurerm_role_assignment.events_dead_letter]
}
