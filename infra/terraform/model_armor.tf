# model_armor.tf — Model Armor guardrail template (rule R1).
#
# General Principle map:
#   P-04 / R1 (guardrail screening): the guardrail adapter (model_armor_guardrail) screens
#         every inbound prompt and outbound dossier through this template for prompt
#         injection, jailbreak, sensitive-data leakage and malicious URLs. Because B1
#         handles customer KYC, this screen is mandatory in both directions.
#   P-03 (residency): the template is created in var.region and called on the regional
#         Model Armor host (modelarmor.<region>.rep.googleapis.com).
#
# verify: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/model_armor_template

resource "google_model_armor_template" "cdd_guardrail" {
  count    = var.standalone ? 1 : 0
  provider = google-beta
  location = var.region # regional endpoint (P-03)
  # Exported as output model_armor_template; the app reads it via CDD_MODEL_ARMOR_TEMPLATE.
  template_id = local.guardrail_template_id

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "LOW_AND_ABOVE"
    }
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
    rai_settings {
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
  }

  # REQUIRED by the API as of the 6.x provider line: creation fails with
  # "The 'template_metadata' field is required." when this block is absent, even though
  # every field inside it is individually optional. Found by execution against the real
  # service on 2026-08-24; `terraform validate` and the offline suite both accept the
  # template without it, because neither resolves the API's own field requirements.
  #
  # The two settings are deliberate, not filler. Multi-language detection matters because a
  # prompt-injection attempt does not have to arrive in English, and enforcement that only
  # reads one language is a guardrail with a documented way around it. Logging only the
  # operations that were BLOCKED keeps the sanitize path from copying customer KYC prompt
  # text into ordinary operation logs, which would put the very material this template exists
  # to protect outside the CMEK-encrypted WORM bucket that is supposed to hold it.
  template_metadata {
    multi_language_detection {
      enable_multi_language_detection = true
    }
    log_sanitize_operations = true
  }

  depends_on = [google_project_service.required]
}
