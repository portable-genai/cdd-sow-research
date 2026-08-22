# signing-key-binding — the apply-time half of the production stage contract.
#
# The reviewed Mode 5 signing key version pinned in `embed_signing_key_version` must equal
# the key version this Terraform configuration actually manages. That comparison can only
# be made once the key-version name is known, so it is an apply-time precondition, and
# proving that it rejects an unbound pin needs a `command = apply` run.
#
# Why this lives in its own module instead of being a fourth precondition on
# terraform_data.production_stage_contract: applying the ROOT module inside `terraform test`
# also creates the CMEK key and the Mode 5 key version, which carry
# `lifecycle { prevent_destroy = true }` on purpose (a destroyed key strands CMEK data and
# a destroyed key version invalidates every issued Mode 5 token). Test teardown then cannot
# destroy them and the whole test file fails after its assertions pass. Keeping this
# contract in a module that declares no cloud resources lets the test apply exactly the
# contract, tear it down cleanly, and leave the production destroy guards in place.

variable "enforced" {
  description = "True only for the production-edge embedded-grant stage, which must run on the reviewed key version."
  type        = bool
}

variable "pinned_key_version" {
  description = "Key-version resource name recorded from the reviewed bootstrap output (empty until one is recorded)."
  type        = string
}

variable "managed_key_version" {
  description = "Key-version resource name Terraform manages; null while no Mode 5 key exists, unknown until it is created."
  type        = string
  nullable    = true
}

resource "terraform_data" "signing_key_binding" {
  input = {
    enforced = var.enforced
    pinned   = var.pinned_key_version
  }

  lifecycle {
    precondition {
      condition     = !var.enforced || var.pinned_key_version == var.managed_key_version
      error_message = "Production Mode 5 requires embed_signing_key_version to equal the exact reviewed Terraform key-version output."
    }
  }
}

output "enforced" {
  description = "Whether this deployment stage is bound to the reviewed key version."
  value       = var.enforced
}
