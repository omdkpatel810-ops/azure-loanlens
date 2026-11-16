output "portal_url" {
  description = "Where applicants go."
  value       = "https://${azurerm_container_app.portal.ingress[0].fqdn}"
}

output "registry" {
  description = "Where CI pushes the portal image."
  value       = azurerm_container_registry.images.login_server
}

output "storage_account" {
  description = "The private document storage."
  value       = azurerm_storage_account.documents.name
}

output "document_intelligence" {
  description = "The endpoint `python -m loanlens.train` and the worker use."
  value       = azurerm_cognitive_account.documents.endpoint
}

output "dashboard_url" {
  description = "Where underwriters go."
  value       = "https://${azurerm_container_app.dashboard.ingress[0].fqdn}"
}
