# Milestone 4: monitoring, alerts, and what the operator needs for the drills.
#
# Application Insights collects requests, traces and logs from all three apps. Local authentication is off,
# so each app sends telemetry signed in as its own identity. Storage reads, writes and deletions are logged
# to the workspace, which is also the audit trail for who opened applicants' documents. Three alerts cover
# what an operator has to act on.

resource "azurerm_application_insights" "loanlens" {
  name                         = "appi-loanlens"
  resource_group_name          = local.rg
  location                     = local.location
  workspace_id                 = azurerm_log_analytics_workspace.apps.id
  application_type             = "web"
  local_authentication_enabled = false # telemetry needs an Entra ID token, not just the connection string
  retention_in_days            = 30
  tags                         = local.tags
}

resource "azurerm_role_assignment" "telemetry" {
  for_each = {
    portal    = azurerm_user_assigned_identity.portal.principal_id
    worker    = azurerm_user_assigned_identity.worker.principal_id
    dashboard = azurerm_user_assigned_identity.dashboard.principal_id
  }

  scope                = azurerm_application_insights.loanlens.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = each.value
}

# Storage logs: every read, write and deletion, with who did it.
resource "azurerm_monitor_diagnostic_setting" "storage" {
  for_each = {
    blob  = "${azurerm_storage_account.documents.id}/blobServices/default"
    queue = "${azurerm_storage_account.documents.id}/queueServices/default"
    table = "${azurerm_storage_account.documents.id}/tableServices/default"
  }

  name                       = "to-log-analytics"
  target_resource_id         = each.value
  log_analytics_workspace_id = azurerm_log_analytics_workspace.apps.id

  dynamic "enabled_log" {
    for_each = ["StorageRead", "StorageWrite", "StorageDelete"]
    content {
      category = enabled_log.value
    }
  }
}

# --- Alerts ---------------------------------------------------------------------------------------------

resource "azurerm_monitor_action_group" "operators" {
  count               = var.alert_email == "" ? 0 : 1
  name                = "ag-loanlens-operators"
  resource_group_name = local.rg
  short_name          = "loanlens"
  tags                = local.tags

  email_receiver {
    name                    = "operator"
    email_address           = var.alert_email
    use_common_alert_schema = true
  }
}

locals {
  alerts = {
    set-aside = {
      description = "The worker set documents aside on the poison queue. Someone has to look at them."
      severity    = 2
      query       = <<-KQL
        traces
        | where message startswith "set aside a message"
        | summarize documents = count()
      KQL
      column      = "documents"
    }
    retries = {
      description = "Reads are being retried: Document Intelligence is throttling or down, and documents are waiting."
      severity    = 2
      query       = <<-KQL
        traces
        | where message startswith "will retry"
        | summarize retries = count()
      KQL
      column      = "retries"
      threshold   = 10
    }
    errors = {
      description = "More than 5% of requests to the portal or dashboard are failing."
      severity    = 1
      query       = <<-KQL
        requests
        | summarize failed = countif(success == false), total = count()
        | where total >= 20
        | extend failure_percent = 100.0 * failed / total
      KQL
      column      = "failure_percent"
      threshold   = 5
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "alert" {
  for_each = local.alerts

  name                 = "loanlens-${each.key}"
  resource_group_name  = local.rg
  location             = local.location
  description          = each.value.description
  severity             = each.value.severity
  scopes               = [azurerm_application_insights.loanlens.id]
  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  tags                 = local.tags

  criteria {
    query                   = each.value.query
    time_aggregation_method = "Maximum"
    metric_measure_column   = each.value.column
    operator                = "GreaterThan"
    threshold               = lookup(each.value, "threshold", 0)

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = var.alert_email == "" ? [] : [1]
    content {
      action_groups = [azurerm_monitor_action_group.operators[0].id]
    }
  }
}

# --- The operator, for the burst test and the failover drill ---------------------------------------------
#
# The burst test starts test applications through the same storage code as the portal, and the failover
# drill writes a probe file every 15 seconds; both run as the operator.

resource "azurerm_role_assignment" "operator_drills" {
  for_each = merge(
    { uploads = { scope = azurerm_storage_container.uploads.id, role = "Storage Blob Data Contributor" } },
    { for t in ["applications", "documents", "readings", "summaries"] : t => {
      scope = azurerm_storage_table.records[t].id, role = "Storage Table Data Contributor"
    } }
  )

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = data.azurerm_client_config.current.object_id
}
