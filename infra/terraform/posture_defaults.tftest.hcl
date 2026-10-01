# posture_defaults.tftest.hcl: the reversible posture controls are OFF unless stated.
#
# Slice 7 of the 2026-09-23 posture rule (2026-10-01): a compliance control that is not
# irreversible defaults off in code, and terraform.tfvars.example carries the production
# form. This file pins that default with mock providers only, like the rest of the suite.

mock_provider "google" {}
mock_provider "google-beta" {}

# Required variables with no default, stated only so the plan runs.
variables {
  human_review_url = "https://review.fictional-bank.example"
  otlp_endpoint    = "https://otel-collector.fictional-bank.example"
}

run "reversible_posture_controls_default_off" {
  command = plan

  variables {
    cmek_enabled                         = true
    project_id                           = "fictional-doc1-production"
    docai_location                       = "us"
    worm_locked                          = false
    deployment_stage                     = "production-edge"
    production_edge_enabled              = true
    compliance_advisory_url              = "https://rm.fictional-bank.example/apps/compliance-advisory/api"
    compliance_advisory_iap_audience     = "1234567890-fictionaledgeclient.apps.googleusercontent.com"
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
    condition     = length(google_access_context_manager_service_perimeter.cdd) == 0
    error_message = "enable_vpc_sc defaults to false: no perimeter unless the deployment states it."
  }

  assert {
    condition     = length(google_org_policy_policy.resource_locations) == 0
    error_message = "enable_org_policies defaults to false: no org policy unless the deployment states it."
  }

  assert {
    condition     = length(google_org_policy_policy.disable_sa_keys) == 0
    error_message = "enable_org_policies defaults to false: no org policy unless the deployment states it."
  }

  assert {
    condition     = length(google_org_policy_policy.uniform_bucket_access) == 0
    error_message = "enable_org_policies defaults to false: no org policy unless the deployment states it."
  }

  assert {
    condition     = length(google_org_policy_policy.allowed_member_domains) == 0
    error_message = "enable_org_policies defaults to false: no org policy unless the deployment states it."
  }
}
