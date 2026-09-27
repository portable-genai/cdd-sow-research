mock_provider "google" {}
mock_provider "google-beta" {}

# The perimeter's one ingress rule: the runtime identities' Model Armor calls, and nothing else.
# A guardrail that the perimeter records as a violation in dry-run is a guardrail the perimeter
# blocks once enforced, so the rule has to ride in both configurations.
variables {
  project_id          = "fictional-doc1-production"
  docai_location      = "us"
  enable_org_policies = false
  enable_vpc_sc       = true
  access_policy_id    = "123456789"
  worm_locked         = false
  model_armor_caller_service_accounts = [
    "journey-a-app-one@fictional-doc1-production.iam.gserviceaccount.com",
    "journey-a-app-two@fictional-doc1-production.iam.gserviceaccount.com",
  ]
}

run "dry_run_admits_the_callers_model_armor_calls_only" {
  command = plan

  assert {
    condition     = length(google_access_context_manager_service_perimeter.cdd[0].spec[0].ingress_policies) == 1
    error_message = "The dry-run spec must carry exactly one ingress rule for the Model Armor callers."
  }

  assert {
    condition = toset(google_access_context_manager_service_perimeter.cdd[0].spec[0].ingress_policies[0].ingress_from[0].identities) == toset([
      "serviceAccount:journey-a-app-one@fictional-doc1-production.iam.gserviceaccount.com",
      "serviceAccount:journey-a-app-two@fictional-doc1-production.iam.gserviceaccount.com",
    ])
    error_message = "The rule must name every caller as a serviceAccount member, and no one else."
  }

  assert {
    condition = (
      length(google_access_context_manager_service_perimeter.cdd[0].spec[0].ingress_policies[0].ingress_to[0].operations) == 1 &&
      google_access_context_manager_service_perimeter.cdd[0].spec[0].ingress_policies[0].ingress_to[0].operations[0].service_name == "modelarmor.googleapis.com"
    )
    error_message = "The rule must admit Model Armor and no other restricted service."
  }

  assert {
    condition     = length(google_access_context_manager_service_perimeter.cdd[0].status[0].ingress_policies) == 0
    error_message = "The open dry-run status restricts nothing, so it carries no rule."
  }

  assert {
    condition     = google_access_context_manager_service_perimeter.cdd[0].use_explicit_dry_run_spec
    error_message = "Naming the callers must never enforce the perimeter."
  }
}

run "enforcement_keeps_the_rule" {
  command = plan

  variables {
    vpc_sc_enforce = true
  }

  assert {
    condition = (
      length(google_access_context_manager_service_perimeter.cdd[0].status[0].ingress_policies) == 1 &&
      google_access_context_manager_service_perimeter.cdd[0].status[0].ingress_policies[0].ingress_to[0].operations[0].service_name == "modelarmor.googleapis.com"
    )
    error_message = "Enforcing the perimeter must not drop the Model Armor ingress rule."
  }
}

run "no_callers_no_rule" {
  command = plan

  variables {
    model_armor_caller_service_accounts = []
  }

  assert {
    condition     = length(google_access_context_manager_service_perimeter.cdd[0].spec[0].ingress_policies) == 0
    error_message = "An empty caller list is a decision for no rule, not a wildcard."
  }
}

run "reject_a_caller_that_is_not_a_service_account" {
  command = plan

  variables {
    model_armor_caller_service_accounts = ["someone@fictional-bank.example"]
  }

  expect_failures = [var.model_armor_caller_service_accounts]
}
