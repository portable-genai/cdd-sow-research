mock_provider "google" {}
mock_provider "google-beta" {}

run "named_edge_contract" {
  command = plan

  variables {
    project_id                           = "fictional-doc1-production"
    docai_location                       = "us"
    enable_org_policies                  = false
    enable_vpc_sc                        = false
    worm_locked                          = false
    deployment_stage                     = "production-edge"
    production_edge_enabled              = true
    api_image                            = "asia-southeast1-docker.pkg.dev/fictional/doc1/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ui_image                             = "asia-southeast1-docker.pkg.dev/fictional/doc1/ui@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    agent_domain                         = "doc1.fictional-bank.example"
    installation_manifest_secret_id      = "doc1-installations"
    installation_manifest_secret_version = "7"
    runtime_settings_secret_id           = "doc1-runtime-settings"
    runtime_settings_secret_version      = "4"
    production_manifest_version          = "test-v1"
    production_manifest_sha256           = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    production_settings_sha256           = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    alert_notification_channels          = ["projects/fictional-doc1-production/notificationChannels/123"]
    production_identity_mode             = "embedded-grant"
    enable_embed_signing_key             = true
    embed_signing_key_version            = "projects/fictional-doc1-production/locations/asia-southeast1/keyRings/cdd-sow-agent-ring/cryptoKeys/cdd-sow-agent-cmek-embed-signing/cryptoKeyVersions/1"
    edge_min_instances                   = 2
    edge_max_instances                   = 4
  }

  assert {
    condition     = google_cloud_run_v2_service.api[0].ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "API must reject direct public Cloud Run ingress."
  }

  assert {
    condition     = google_cloud_run_v2_service.ui[0].ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "UI must reject direct public Cloud Run ingress."
  }

  assert {
    condition     = length(google_compute_backend_service.api[0].iap) == 0 && length(google_compute_backend_service.ui[0].iap) == 0
    error_message = "Sandboxed Modes 4/5 backends must not put an IAP redirect in front of the loader or iframe."
  }

  assert {
    condition     = length(google_kms_crypto_key_version.embed_signing) == 1
    error_message = "Mode 5 must provision exactly one initial non-exportable signing version."
  }

  assert {
    condition     = module.embed_signing_key_binding.enforced
    error_message = "The production embedded-grant edge must enforce the reviewed key-version binding."
  }

  assert {
    condition     = google_firestore_index.browser_flow_pending_outbox.collection == "browser_flow_outbox"
    error_message = "The transactional browser-flow outbox requires its composite pending-event index."
  }

  assert {
    condition     = endswith(google_cloud_run_v2_service.api[0].template[0].containers[0].image, "@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    error_message = "API image must remain the reviewed digest."
  }

  assert {
    condition     = one([for item in google_cloud_run_v2_service.api[0].template[0].containers[0].env : item.value if item.name == "GOOGLE_CLOUD_PROJECT"]) == "fictional-doc1-production"
    error_message = "The API project must come from the applied infrastructure, not a settings default."
  }

  assert {
    condition     = one([for item in google_cloud_run_v2_service.api[0].template[0].containers[0].env : item.value if item.name == "GCP_REGION"]) == var.region
    error_message = "The API region must equal the Terraform deployment region."
  }

  assert {
    condition     = one([for item in google_cloud_run_v2_service.api[0].template[0].containers[0].env : item.value if item.name == "CDD_EXPECTED_MANIFEST_SHA256"]) == var.production_manifest_sha256
    error_message = "The API must receive the exact reviewed manifest digest."
  }

  assert {
    condition     = one([for item in google_cloud_run_v2_service.api[0].template[0].containers[0].env : item.value if item.name == "CDD_EXPECTED_SETTINGS_SHA256"]) == var.production_settings_sha256
    error_message = "The API must receive the exact reviewed settings digest."
  }

  assert {
    condition = (
      one([for item in google_cloud_run_v2_service.ui[0].template[0].containers[0].env : item.value if item.name == "CDD_EXPECTED_MANIFEST_SHA256"]) == var.production_manifest_sha256 &&
      one([for item in google_cloud_run_v2_service.ui[0].template[0].containers[0].env : item.value if item.name == "CDD_EXPECTED_SETTINGS_SHA256"]) == var.production_settings_sha256 &&
      one([for item in google_cloud_run_v2_service.ui[0].template[0].containers[0].env : item.value if item.name == "CDD_PRODUCTION"]) == "true"
    )
    error_message = "The production UI must require both reviewed payload digests."
  }

  assert {
    condition     = one([for item in google_cloud_run_v2_service.api[0].template[0].containers[0].env : item.value if item.name == "CDD_FIRESTORE_DB"]) == google_firestore_database.sow_cases.name
    error_message = "The case store must use the named regional database provisioned by Terraform."
  }

  assert {
    condition     = google_cloud_run_v2_service.ui[0].template[0].containers[0].startup_probe[0].http_get[0].path == "/agent/ready"
    error_message = "UI readiness must parse and validate the mounted installation manifest."
  }

  assert {
    condition     = length(google_compute_security_policy.api_per_source) == 1
    error_message = "The named edge must provision its per-source Cloud Armor abuse boundary."
  }

  assert {
    condition     = one([for rule in google_compute_security_policy.api_per_source[0].rule : rule.rate_limit_options[0].rate_limit_threshold[0].count if rule.action == "throttle"]) == 120
    error_message = "The per-source API throttle must retain the reviewed requests-per-minute ceiling."
  }

  assert {
    condition     = one([for item in google_cloud_run_v2_service.api[0].template[0].containers[0].env : item.value if item.name == "CDD_EMBED_SHARED_RATE_LIMIT_MAX"]) == "600"
    error_message = "The API must receive the reviewed shared broker-capacity backstop."
  }

  assert {
    condition     = google_compute_url_map.agent[0].path_matcher[0].route_rules[0].route_action[0].url_rewrite[0].path_prefix_rewrite == "/"
    error_message = "The /agent/api route must strip its public prefix before FastAPI."
  }
}

run "default_omits_irreversible_mode5_key" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    worm_locked         = false
  }

  assert {
    condition     = length(google_kms_crypto_key.embed_signing) == 0
    error_message = "The irreversible Mode 5 signing key must be opt-in."
  }
}

run "mode5_key_bootstrap_omits_edge" {
  command = plan

  variables {
    project_id                     = "fictional-doc1-production"
    docai_location                 = "us"
    enable_org_policies            = false
    enable_vpc_sc                  = false
    worm_locked                    = false
    deployment_stage               = "mode5-key-bootstrap"
    production_edge_enabled        = false
    production_identity_mode       = "embedded-grant"
    enable_embed_signing_key       = true
    embed_signing_protection_level = "HSM"
  }

  assert {
    condition     = length(google_kms_crypto_key.embed_signing) == 1
    error_message = "Bootstrap must create the reviewed HSM signing key."
  }

  assert {
    condition     = length(google_cloud_run_v2_service.api) == 0 && length(google_cloud_run_v2_service.ui) == 0
    error_message = "Bootstrap must not create the production edge before reviewed runtime settings exist."
  }
}

run "reject_bootstrap_with_edge_enabled" {
  command = plan

  variables {
    project_id                           = "fictional-doc1-production"
    docai_location                       = "us"
    enable_org_policies                  = false
    enable_vpc_sc                        = false
    worm_locked                          = false
    deployment_stage                     = "mode5-key-bootstrap"
    production_edge_enabled              = true
    api_image                            = "asia-southeast1-docker.pkg.dev/fictional/doc1/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ui_image                             = "asia-southeast1-docker.pkg.dev/fictional/doc1/ui@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    agent_domain                         = "doc1.fictional-bank.example"
    installation_manifest_secret_id      = "doc1-installations"
    installation_manifest_secret_version = "7"
    runtime_settings_secret_id           = "doc1-runtime-settings"
    runtime_settings_secret_version      = "4"
    production_manifest_version          = "test-v1"
    production_manifest_sha256           = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    production_settings_sha256           = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    alert_notification_channels          = ["projects/fictional-doc1-production/notificationChannels/123"]
    production_identity_mode             = "embedded-grant"
    enable_embed_signing_key             = true
    embed_signing_key_version            = "projects/fictional-doc1-production/locations/asia-southeast1/keyRings/cdd-sow-agent-ring/cryptoKeys/cdd-sow-agent-cmek-embed-signing/cryptoKeyVersions/1"
    embed_signing_protection_level       = "HSM"
    edge_min_instances                   = 2
    edge_max_instances                   = 4
  }

  expect_failures = [terraform_data.production_stage_contract]
}

# This run block deliberately sets NEITHER retention_days NOR worm_locked: it exists to observe
# the shipped defaults in variables.tf, so pinning either one would make its own assertions
# tautological. That makes it the one guard here sensitive to an auto-loaded terraform.tfvars —
# `terraform test` loads that filename silently, so real deployment inputs must NOT use it.
# Keep operator inputs in an explicitly-passed file (e.g. `-var-file=doc1-prod.tfvars`).
run "default_retention_keeps_worm_lock_enabled" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    standalone          = false
  }

  assert {
    condition     = var.retention_days == 180
    error_message = "The audit-retention default must remain six months."
  }

  assert {
    condition     = var.worm_locked
    error_message = "Changing the retention default must not disable the irreversible WORM lock."
  }
}

run "reject_retention_below_six_months" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    standalone          = false
    worm_locked         = true
    retention_days      = 179
  }

  expect_failures = [var.retention_days]
}

run "unlocked_stack_accepts_short_retention" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    standalone          = false
    worm_locked         = false
    retention_days      = 3
  }

  assert {
    condition     = var.retention_days == 3 && !var.worm_locked
    error_message = "An UNLOCKED evaluation stack must be allowed a short, destroyable retention."
  }
}

run "unlocked_stack_still_requires_a_positive_retention" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    standalone          = false
    worm_locked         = false
    retention_days      = 0
  }

  expect_failures = [var.retention_days]
}

run "reject_reducing_existing_locked_retention" {
  command = plan

  variables {
    project_id                     = "fictional-doc1-production"
    docai_location                 = "us"
    enable_org_policies            = false
    enable_vpc_sc                  = false
    standalone                     = false
    retention_days                 = 180
    existing_locked_retention_days = 2557
  }

  expect_failures = [var.existing_locked_retention_days]
}

run "reject_mutable_api_image" {
  command = plan

  variables {
    project_id                           = "fictional-doc1-production"
    docai_location                       = "us"
    enable_org_policies                  = false
    enable_vpc_sc                        = false
    worm_locked                          = false
    deployment_stage                     = "production-edge"
    production_edge_enabled              = true
    api_image                            = "asia-southeast1-docker.pkg.dev/fictional/doc1/api:latest"
    ui_image                             = "asia-southeast1-docker.pkg.dev/fictional/doc1/ui@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    agent_domain                         = "doc1.fictional-bank.example"
    installation_manifest_secret_id      = "doc1-installations"
    installation_manifest_secret_version = "7"
    runtime_settings_secret_id           = "doc1-runtime-settings"
    runtime_settings_secret_version      = "4"
    production_manifest_version          = "test-v1"
    production_manifest_sha256           = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    production_settings_sha256           = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    alert_notification_channels          = ["projects/fictional-doc1-production/notificationChannels/123"]
    edge_min_instances                   = 2
  }

  expect_failures = [var.api_image]
}

run "reject_reserved_secret_override" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    worm_locked         = false
    additional_secret_env = {
      GOOGLE_CLOUD_PROJECT = {
        secret_id = "wrong-project"
        version   = "1"
      }
    }
  }

  expect_failures = [var.additional_secret_env]
}

run "reject_region_secret_override" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    docai_location      = "us"
    enable_org_policies = false
    enable_vpc_sc       = false
    worm_locked         = false
    additional_secret_env = {
      GCP_REGION = {
        secret_id = "wrong-region"
        version   = "1"
      }
    }
  }

  expect_failures = [var.additional_secret_env]
}

run "reject_edge_limit_that_can_exhaust_shared_capacity" {
  command = plan

  variables {
    project_id                            = "fictional-doc1-production"
    docai_location                        = "us"
    enable_org_policies                   = false
    enable_vpc_sc                         = false
    worm_locked                           = false
    edge_per_source_rate_limit_per_minute = 10000
    embed_shared_rate_limit_max           = 100
  }

  expect_failures = [var.embed_shared_rate_limit_max]
}

# The key-version binding is an apply-time contract: the pinned version can only be
# compared once the managed key version exists, and a `command = plan` run leaves that
# comparison unknown, so it proves nothing. Applying the ROOT module here would instead put
# google_kms_crypto_key.cdd and google_kms_crypto_key_version.embed_signing (both
# lifecycle.prevent_destroy, deliberately) into test state, which teardown then cannot
# destroy. The two runs below therefore apply modules/signing-key-binding directly: same
# contract, real apply-time evidence, and nothing in test state that refuses to be
# destroyed. The root wiring of that module is asserted in "named_edge_contract".
run "reject_unbound_production_signing_key_version" {
  command = plan

  module {
    source = "./modules/signing-key-binding"
  }

  variables {
    enforced            = true
    pinned_key_version  = "projects/fictional-doc1-production/locations/asia-southeast1/keyRings/cdd-sow-agent-ring/cryptoKeys/cdd-sow-agent-cmek-embed-signing/cryptoKeyVersions/2"
    managed_key_version = "projects/fictional-doc1-production/locations/asia-southeast1/keyRings/cdd-sow-agent-ring/cryptoKeys/cdd-sow-agent-cmek-embed-signing/cryptoKeyVersions/1"
  }

  expect_failures = [terraform_data.signing_key_binding]
}

run "accept_reviewed_production_signing_key_version" {
  command = apply

  module {
    source = "./modules/signing-key-binding"
  }

  variables {
    enforced            = true
    pinned_key_version  = "projects/fictional-doc1-production/locations/asia-southeast1/keyRings/cdd-sow-agent-ring/cryptoKeys/cdd-sow-agent-cmek-embed-signing/cryptoKeyVersions/1"
    managed_key_version = "projects/fictional-doc1-production/locations/asia-southeast1/keyRings/cdd-sow-agent-ring/cryptoKeys/cdd-sow-agent-cmek-embed-signing/cryptoKeyVersions/1"
  }

  assert {
    condition     = terraform_data.signing_key_binding.output.enforced
    error_message = "The reviewed production stage must stay bound to the Terraform-managed key version."
  }
}

run "unbound_stage_ignores_key_version_pin" {
  command = apply

  module {
    source = "./modules/signing-key-binding"
  }

  variables {
    enforced            = false
    pinned_key_version  = ""
    managed_key_version = null
  }

  assert {
    condition     = terraform_data.signing_key_binding.output.enforced == false
    error_message = "Stages without an embedded-grant production edge must not require a pinned key version."
  }
}

# Until 2026-08-28 this variable DERIVED the processor location from var.region, while the
# runtime routed to `us`: Terraform would have created the processor in one location and the
# adapter looked for it in another, a 404 at request time rather than at apply. The variable
# now takes the fleet rule (the deploy region or a named multi-region, `global` refused by
# name), so the refusal moves from a resource precondition to the variable validation.
run "reject_an_unlocated_docai_location" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    enable_org_policies = false
    enable_vpc_sc       = false
    worm_locked         = false
    region              = "asia-southeast1"
    allowed_regions     = ["asia-southeast1"]
    docai_location      = "global"
  }

  expect_failures = [var.docai_location]
}

# Another single region is refused by the same condition: it is neither the deploy region
# nor a multi-region commitment, so accepting it would be a silent jurisdiction change
# dressed as a fix.
run "reject_a_foreign_single_region_docai_location" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    enable_org_policies = false
    enable_vpc_sc       = false
    worm_locked         = false
    region              = "asia-southeast1"
    allowed_regions     = ["asia-southeast1"]
    docai_location      = "europe-west2"
  }

  expect_failures = [var.docai_location]
}

# The deploy region itself is allowed and used unchanged, so an in-country deployment never
# widens by accident. asia-southeast1 is served (access-gated), which is worth pinning: it is
# the region the alignment record chose, and setting it here is the whole migration the day
# Google grants single-region access.
run "a_served_region_needs_no_widening" {
  command = plan

  variables {
    project_id          = "fictional-doc1-production"
    enable_org_policies = false
    enable_vpc_sc       = false
    worm_locked         = false
    region              = "asia-southeast1"
    allowed_regions     = ["asia-southeast1"]
    docai_location      = "asia-southeast1"
  }

  assert {
    condition     = google_document_ai_processor.kyc.location == "asia-southeast1"
    error_message = "The deploy region must be used as-is, never widened to a multi-region."
  }
}
