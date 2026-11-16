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

# Milestone 2: the worker (worker.tf)

variable "document_intelligence_sku" {
  description = "F0 is the free tier, with monthly limits; S0 is pay as you go."
  type        = string
  default     = "S0"

  validation {
    condition     = contains(["F0", "S0"], var.document_intelligence_sku)
    error_message = "F0 or S0."
  }
}

variable "document_model_id" {
  description = "The custom T4 model the worker uses, as trained by `python -m loanlens.train`."
  type        = string
  default     = "loanlens-t4-v1"

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9._~-]{1,63}$", var.document_model_id))
    error_message = "A Document Intelligence model ID: letters, digits and . _ ~ -, up to 64 characters."
  }
}

variable "confidence_threshold" {
  description = "Below this confidence, a value the model read sends the document to a person."
  type        = number
  default     = 0.8

  validation {
    condition     = var.confidence_threshold >= 0.5 && var.confidence_threshold < 1
    error_message = "Between 0.5 and 1."
  }
}

# Milestone 3: the underwriter dashboard (dashboard.tf)

variable "underwriters" {
  description = "Staff who get the Underwriter role, by sign-in name (for example sam@yourcreditunion.ca)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for u in var.underwriters : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", u))])
    error_message = "Sign-in names look like name@domain."
  }
}

variable "staff_ip_ranges" {
  description = "If set, the dashboard answers only these addresses, such as the credit union's offices. Empty: anywhere, behind sign-in."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for r in var.staff_ip_ranges : can(cidrhost(r, 0)) && r != "0.0.0.0/0"])
    error_message = "CIDR ranges, never 0.0.0.0/0."
  }
}

variable "openai_location" {
  description = "Where Azure OpenAI runs. Canada East has the models; Canada Central has few."
  type        = string
  default     = "canadaeast"

  validation {
    condition     = contains(["canadacentral", "canadaeast"], var.openai_location)
    error_message = "Canada only: canadacentral or canadaeast (Guardrails refuses anything else)."
  }
}

variable "openai_model" {
  description = "The model that writes summaries. Check it's offered as Standard in openai_location."
  type        = string
  default     = "gpt-4o-mini"
}

variable "openai_model_version" {
  description = "The model's version."
  type        = string
  default     = "2024-07-18"
}

variable "openai_capacity" {
  description = "Thousands of tokens a minute. A summary is about 600 tokens, so 10 is about 15 summaries a minute."
  type        = number
  default     = 10

  validation {
    condition     = var.openai_capacity >= 1 && var.openai_capacity <= 100
    error_message = "Between 1 and 100."
  }
}
