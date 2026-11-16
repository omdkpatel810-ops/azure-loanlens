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

run "telemetry_needs_each_apps_identity" {
  command = apply

  assert {
    condition     = azurerm_application_insights.loanlens.local_authentication_enabled == false && azurerm_application_insights.loanlens.workspace_id == azurerm_log_analytics_workspace.apps.id
    error_message = "Application Insights, in LoanLens' workspace, accepting only Entra ID-signed telemetry."
  }

  assert {
    condition     = toset(keys(azurerm_role_assignment.telemetry)) == toset(["portal", "worker", "dashboard"]) && alltrue([for r in values(azurerm_role_assignment.telemetry) : r.role_definition_name == "Monitoring Metrics Publisher"])
    error_message = "Each app may publish telemetry, and only that."
  }

  assert {
    condition = alltrue([for app in [azurerm_container_app.portal, azurerm_container_app.worker, azurerm_container_app.dashboard] :
      contains([for e in app.template[0].container[0].env : e.secret_name if e.name == "APPLICATIONINSIGHTS_CONNECTION_STRING"], "appinsights-connection")
    ])
    error_message = "All three apps know where to send telemetry, from a secret rather than plain settings."
  }
}

run "storage_access_is_logged" {
  command = plan

  assert {
    condition     = toset(keys(azurerm_monitor_diagnostic_setting.storage)) == toset(["blob", "queue", "table"])
    error_message = "Blob, queue and table services all logged."
  }

  assert {
    condition     = toset([for l in azurerm_monitor_diagnostic_setting.storage["blob"].enabled_log : l.category]) == toset(["StorageRead", "StorageWrite", "StorageDelete"])
    error_message = "Every read of an applicant's document is logged, with who read it."
  }
}

run "three_alerts_and_nobody_emailed_by_default" {
  command = plan

  assert {
    condition     = toset(keys(azurerm_monitor_scheduled_query_rules_alert_v2.alert)) == toset(["set-aside", "retries", "errors"])
    error_message = "Poisoned documents, retries piling up, and failing requests."
  }

  assert {
    condition     = strcontains(azurerm_monitor_scheduled_query_rules_alert_v2.alert["set-aside"].criteria[0].query, "set aside a message") && azurerm_monitor_scheduled_query_rules_alert_v2.alert["set-aside"].criteria[0].threshold == 0
    error_message = "The set-aside alert matches what the worker logs, and fires on the first one."
  }

  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.alert["errors"].criteria[0].threshold == 5 && azurerm_monitor_scheduled_query_rules_alert_v2.alert["errors"].severity == 1
    error_message = "More than 5% failing requests is the most urgent alert."
  }

  assert {
    condition     = length(azurerm_monitor_action_group.operators) == 0 && length(azurerm_monitor_scheduled_query_rules_alert_v2.alert["errors"].action) == 0
    error_message = "Without an email address, alerts still fire but email nobody."
  }
}

run "alerts_emailed_when_asked" {
  command = plan

  variables {
    alert_email = "ops@example.com"
  }

  assert {
    condition     = one(azurerm_monitor_action_group.operators[0].email_receiver).email_address == "ops@example.com" && length(azurerm_monitor_scheduled_query_rules_alert_v2.alert["retries"].action) == 1
    error_message = "Every alert emails the operator."
  }
}

run "the_operator_can_run_the_drills" {
  command = plan

  assert {
    condition     = azurerm_role_assignment.operator_drills["uploads"].role_definition_name == "Storage Blob Data Contributor" && azurerm_role_assignment.operator_drills["applications"].role_definition_name == "Storage Table Data Contributor"
    error_message = "The burst test and the failover probes write as the operator."
  }
}

run "refuses_a_bad_alert_email" {
  command = plan

  variables {
    alert_email = "ops"
  }

  expect_failures = [var.alert_email]
}
