variable "resource_group_name" {
  description = "LoanLens' resource group, vended by Guardrails from environments/loanlens-prod.json."
  type        = string
  default     = "rg-loanlens-prod"
}

variable "address_space" {
  description = "LoanLens' network, inside Guardrails' 10.100.0.0/16 plan."
  type        = string
  default     = "10.100.6.0/24"

  validation {
    condition     = can(cidrhost(var.address_space, 0)) && tonumber(split("/", var.address_space)[1]) <= 26 && startswith(var.address_space, "10.100.")
    error_message = "A /26 or bigger inside 10.100.0.0/16."
  }
}

variable "operator_ip_ranges" {
  description = "Addresses Terraform runs from. The storage account refuses every other address outside LoanLens' own network."
  type        = list(string)

  validation {
    condition     = length(var.operator_ip_ranges) > 0 && alltrue([for r in var.operator_ip_ranges : can(cidrhost(r, 0)) && !contains(["0.0.0.0/0", "0.0.0.0"], r)])
    error_message = "List one or more CIDR ranges, never 0.0.0.0/0."
  }
}

variable "customer_tenant_id" {
  description = "The Microsoft Entra External ID tenant applicants sign in with (its tenant ID)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.customer_tenant_id))
    error_message = "A tenant ID, like 00000000-0000-0000-0000-000000000000."
  }
}

variable "customer_tenant_subdomain" {
  description = "The External ID tenant's subdomain: \"loanlensapplicants\" for loanlensapplicants.ciamlogin.com."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,26}$", var.customer_tenant_subdomain))
    error_message = "3 to 26 lowercase letters and digits."
  }
}

variable "image_tag" {
  description = "The portal image to run, from LoanLens' container registry. Empty on the very first apply, before any image exists: a placeholder runs instead."
  type        = string
  default     = ""

  validation {
    condition     = var.image_tag == "" || can(regex("^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$", var.image_tag))
    error_message = "A container image tag, such as a commit SHA."
  }
}

variable "document_retention_days" {
  description = "How long uploaded documents are kept. Seven years by default; check the credit union's own retention rules."
  type        = number
  default     = 2555

  validation {
    condition     = var.document_retention_days >= 365 && var.document_retention_days <= 3650
    error_message = "Between 1 and 10 years."
  }
}
