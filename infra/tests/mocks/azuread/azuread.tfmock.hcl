# Shared stand-ins for Entra ID (both the staff directory and the applicants' External ID tenant).

mock_resource "azuread_application" {
  defaults = {
    id        = "/applications/00000000-0000-0000-0000-00000000dddd"
    client_id = "00000000-0000-0000-0000-00000000eeee"
  }
}

mock_resource "azuread_application_password" {
  defaults = { value = "mock-secret-value" }
}

mock_resource "azuread_service_principal" {
  defaults = { object_id = "00000000-0000-0000-0000-00000000f00d" }
}

mock_data "azuread_user" {
  defaults = { object_id = "00000000-0000-0000-0000-0000000005a1" }
}
