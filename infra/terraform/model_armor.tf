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

  depends_on = [google_project_service.required]
}
