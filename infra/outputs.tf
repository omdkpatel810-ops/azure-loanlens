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
