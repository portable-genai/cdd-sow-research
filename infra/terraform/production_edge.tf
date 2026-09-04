# Named production edge for the fixed /agent artifact and same-origin /agent/api contract.
#
# The Cloud Run services accept traffic only from internal sources and the external
# Application Load Balancer. Their IAM permits invocation because serverless NEGs do not
# present an application identity, but direct run.app ingress is still rejected. Modes 4/5
# authenticate inside the application: putting IAP in front of the loader and iframe would
# break cross-origin embedding. A separate standalone Mode 6 edge may add IAP.

resource "terraform_data" "production_stage_contract" {
  input = {
    stage               = var.deployment_stage
    edge_enabled        = var.production_edge_enabled
    identity_mode       = var.production_identity_mode
    signing_key_enabled = var.enable_embed_signing_key
  }

  lifecycle {
    precondition {
      condition = (
        var.deployment_stage == "disabled" ?
        (!var.production_edge_enabled && !var.enable_embed_signing_key) :
        var.deployment_stage == "mode5-key-bootstrap" ?
        (!var.production_edge_enabled && var.production_identity_mode == "embedded-grant" && var.enable_embed_signing_key) :
        (var.production_edge_enabled && var.enable_embed_signing_key == (var.production_identity_mode == "embedded-grant"))
      )
      error_message = "disabled requires edge=false/key=false; mode5-key-bootstrap requires edge=false, embedded-grant and key=true; production-edge requires edge=true and a key only for embedded-grant."
    }

    precondition {
      condition     = !var.enable_embed_signing_key || var.embed_signing_protection_level == "HSM"
      error_message = "Every reviewed Mode 5 signing key stage requires HSM protection."
    }
  }
}

# The remaining stage rule, "the pinned key version must be the one Terraform manages", can
# only be evaluated once the key-version name is known, so it is an apply-time precondition
# and lives in its own resource-free module. That keeps it provable by `terraform test` runs
# against that module alone, without dragging the prevent_destroy CMEK and Mode 5 key
# resources into test state (see modules/signing-key-binding/main.tf).
module "embed_signing_key_binding" {
  source = "./modules/signing-key-binding"

  enforced            = var.deployment_stage == "production-edge" && var.production_identity_mode == "embedded-grant"
  pinned_key_version  = var.embed_signing_key_version
  managed_key_version = one(google_kms_crypto_key_version.embed_signing[*].name)
}

resource "google_service_account" "ui" {
  count        = var.production_edge_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${local.app_sa_id}-ui"
  display_name = "cdd-sow-research portable UI serving identity"

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "ui_manifest" {
  count     = var.production_edge_enabled ? 1 : 0
  project   = var.project_id
  secret_id = var.installation_manifest_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ui[0].email}"
}

resource "google_secret_manager_secret_iam_member" "api_manifest" {
  count     = var.production_edge_enabled ? 1 : 0
  project   = var.project_id
  secret_id = var.installation_manifest_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "api_settings" {
  count     = var.production_edge_enabled ? 1 : 0
  project   = var.project_id
  secret_id = var.runtime_settings_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "api_env" {
  for_each = var.production_edge_enabled ? var.additional_secret_env : {}

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_cloud_run_v2_service" "api" {
  count    = var.production_edge_enabled ? 1 : 0
  project  = var.project_id
  name     = "${var.name_prefix}-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  deletion_protection = var.cloud_run_deletion_protection

  template {
    service_account = google_service_account.app.email

    scaling {
      min_instance_count = var.edge_min_instances
      max_instance_count = var.edge_max_instances
    }

    containers {
      image = var.api_image

      ports {
        container_port = 8090
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "CDD_PROFILE"
        value = "gcp"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "CDD_FIRESTORE_DB"
        value = google_firestore_database.sow_cases.name
      }
      env {
        name  = "CDD_KMS_KEY"
        value = google_kms_crypto_key.cdd.id
      }
      env {
        name  = "CDD_DOCAI_PROCESSOR"
        value = google_document_ai_processor.kyc.id
      }
      env {
        name  = "CDD_IDENTITY_PROFILE"
        value = var.production_identity_mode
      }
      env {
        name  = "CDD_CHANNEL_PROFILE"
        value = "sandboxed"
      }
      env {
        name  = "CDD_PUBLIC_ORIGIN"
        value = "https://${var.agent_domain}"
      }
      env {
        name  = "CDD_INSTALLATION_MANIFEST"
        value = "/secrets/installations/installations.json"
      }
      env {
        name  = "CDD_INSTALLATION_MANIFEST_VERSION"
        value = var.production_manifest_version
      }
      env {
        name  = "CDD_EXPECTED_MANIFEST_SHA256"
        value = var.production_manifest_sha256
      }
      env {
        name  = "CDD_SETTINGS"
        value = "/secrets/settings/settings.yaml"
      }
      env {
        name  = "CDD_EXPECTED_SETTINGS_SHA256"
        value = var.production_settings_sha256
      }
      env {
        name  = "CDD_PRODUCTION"
        value = "true"
      }
      env {
        name  = "CDD_REPLICA_COUNT"
        value = tostring(var.edge_min_instances)
      }
      env {
        name  = "CDD_BROWSER_FLOW_FIRESTORE_DB"
        value = google_firestore_database.sow_cases.name
      }
      env {
        name  = "CDD_BROWSER_FLOW_COLLECTION"
        value = var.browser_flow_records_collection
      }
      env {
        name  = "CDD_BROWSER_FLOW_ALIAS_COLLECTION"
        value = var.browser_flow_aliases_collection
      }
      env {
        name  = "CDD_BROWSER_FLOW_OUTBOX_COLLECTION"
        value = var.browser_flow_outbox_collection
      }
      env {
        name  = "CDD_CITATION_LEDGER_COLLECTION"
        value = var.browser_flow_citations_collection
      }
      env {
        name  = "CDD_CLIENT_ASSERTION_REPLAY_COLLECTION"
        value = var.browser_flow_replay_collection
      }
      env {
        name  = "CDD_EMBED_RATE_LIMIT_COLLECTION"
        value = var.embed_rate_limits_collection
      }
      env {
        name  = "CDD_EMBED_SHARED_RATE_LIMIT_MAX"
        value = tostring(var.embed_shared_rate_limit_max)
      }
      env {
        name  = "CDD_EMBED_SHARED_RATE_LIMIT_WINDOW"
        value = "60"
      }

      dynamic "env" {
        for_each = var.additional_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = env.value.version
            }
          }
        }
      }

      volume_mounts {
        name       = "installations"
        mount_path = "/secrets/installations"
      }
      volume_mounts {
        name       = "settings"
        mount_path = "/secrets/settings"
      }

      startup_probe {
        failure_threshold     = 12
        initial_delay_seconds = 5
        period_seconds        = 5
        timeout_seconds       = 3
        http_get {
          path = "/healthz"
          port = 8090
        }
      }

      liveness_probe {
        period_seconds  = 30
        timeout_seconds = 5
        http_get {
          path = "/healthz"
          port = 8090
        }
      }
    }

    volumes {
      name = "installations"
      secret {
        secret = var.installation_manifest_secret_id
        items {
          version = var.installation_manifest_secret_version
          path    = "installations.json"
        }
      }
    }

    volumes {
      name = "settings"
      secret {
        secret = var.runtime_settings_secret_id
        items {
          version = var.runtime_settings_secret_version
          path    = "settings.yaml"
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.api_manifest,
    google_secret_manager_secret_iam_member.api_settings,
    google_secret_manager_secret_iam_member.api_env,
  ]
}

resource "google_cloud_run_v2_service" "ui" {
  count    = var.production_edge_enabled ? 1 : 0
  project  = var.project_id
  name     = "${var.name_prefix}-ui"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  deletion_protection = var.cloud_run_deletion_protection

  template {
    service_account = google_service_account.ui[0].email

    scaling {
      min_instance_count = var.edge_min_instances
      max_instance_count = var.edge_max_instances
    }

    containers {
      image = var.ui_image

      ports {
        container_port = 3000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "CDD_INSTALLATION_MANIFEST"
        value = "/secrets/installations/installations.json"
      }
      env {
        name  = "CDD_EXPECTED_MANIFEST_SHA256"
        value = var.production_manifest_sha256
      }
      env {
        name  = "CDD_EXPECTED_SETTINGS_SHA256"
        value = var.production_settings_sha256
      }
      env {
        name  = "CDD_PRODUCTION"
        value = "true"
      }

      volume_mounts {
        name       = "installations"
        mount_path = "/secrets/installations"
      }

      startup_probe {
        failure_threshold     = 12
        initial_delay_seconds = 5
        period_seconds        = 5
        timeout_seconds       = 3
        http_get {
          path = "/agent/ready"
          port = 3000
        }
      }
    }

    volumes {
      name = "installations"
      secret {
        secret = var.installation_manifest_secret_id
        items {
          version = var.installation_manifest_secret_version
          path    = "installations.json"
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.ui_manifest]
}

resource "google_cloud_run_v2_service_iam_member" "api_load_balancer" {
  count    = var.production_edge_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "ui_load_balancer" {
  count    = var.production_edge_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ui[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_compute_region_network_endpoint_group" "api" {
  count                 = var.production_edge_enabled ? 1 : 0
  project               = var.project_id
  name                  = "${var.name_prefix}-api-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.api[0].name
  }
}

resource "google_compute_region_network_endpoint_group" "ui" {
  count                 = var.production_edge_enabled ? 1 : 0
  project               = var.project_id
  name                  = "${var.name_prefix}-ui-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.ui[0].name
  }
}

resource "google_compute_backend_service" "api" {
  count                 = var.production_edge_enabled ? 1 : 0
  project               = var.project_id
  name                  = "${var.name_prefix}-api"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.api_per_source[0].id

  backend {
    group = google_compute_region_network_endpoint_group.api[0].id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_security_policy" "api_per_source" {
  count   = var.production_edge_enabled ? 1 : 0
  project = var.project_id
  name    = "${var.name_prefix}-api-rate-limit"

  rule {
    action   = "throttle"
    priority = 1000

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"

      rate_limit_threshold {
        count        = var.edge_per_source_rate_limit_per_minute
        interval_sec = 60
      }
    }

    description = "Per-source abuse boundary; shared Firestore counters remain a capacity backstop."
  }

  rule {
    action   = "allow"
    priority = 2147483647

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }

    description = "Default allow after the API-wide per-source throttle."
  }
}

resource "google_compute_backend_service" "ui" {
  count                 = var.production_edge_enabled ? 1 : 0
  project               = var.project_id
  name                  = "${var.name_prefix}-ui"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  backend {
    group = google_compute_region_network_endpoint_group.ui[0].id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_url_map" "agent" {
  count           = var.production_edge_enabled ? 1 : 0
  project         = var.project_id
  name            = "${var.name_prefix}-edge"
  default_service = google_compute_backend_service.ui[0].id

  host_rule {
    hosts        = [var.agent_domain]
    path_matcher = "agent"
  }

  path_matcher {
    name            = "agent"
    default_service = google_compute_backend_service.ui[0].id

    route_rules {
      priority = 10
      service  = google_compute_backend_service.api[0].id

      match_rules {
        prefix_match = "/agent/api/"
      }

      route_action {
        url_rewrite {
          path_prefix_rewrite = "/"
        }
      }
    }

    route_rules {
      priority = 20
      service  = google_compute_backend_service.api[0].id

      match_rules {
        prefix_match = "/agent/auth/"
      }

      route_action {
        url_rewrite {
          path_prefix_rewrite = "/auth/"
        }
      }
    }
  }
}

resource "google_compute_managed_ssl_certificate" "agent" {
  count   = var.production_edge_enabled ? 1 : 0
  project = var.project_id
  name    = "${var.name_prefix}-agent"

  managed {
    domains = [var.agent_domain]
  }
}

resource "google_compute_target_https_proxy" "agent" {
  count            = var.production_edge_enabled ? 1 : 0
  project          = var.project_id
  name             = "${var.name_prefix}-agent"
  url_map          = google_compute_url_map.agent[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.agent[0].id]
}

resource "google_compute_global_address" "agent" {
  count   = var.production_edge_enabled ? 1 : 0
  project = var.project_id
  name    = "${var.name_prefix}-agent"
}

resource "google_compute_global_forwarding_rule" "agent" {
  count                 = var.production_edge_enabled ? 1 : 0
  project               = var.project_id
  name                  = "${var.name_prefix}-agent-https"
  target                = google_compute_target_https_proxy.agent[0].id
  port_range            = "443"
  ip_address            = google_compute_global_address.agent[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

resource "google_dns_record_set" "agent" {
  count        = var.production_edge_enabled && var.dns_managed_zone != "" ? 1 : 0
  project      = var.project_id
  managed_zone = var.dns_managed_zone
  name         = "${var.agent_domain}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.agent[0].address]
}

output "production_agent_origin" {
  description = "Named production origin. Empty when production_edge_enabled is false."
  value       = var.production_edge_enabled ? "https://${var.agent_domain}" : ""
}

output "production_api_service" {
  description = "Cloud Run API service receiving only load-balancer/internal ingress."
  value       = try(google_cloud_run_v2_service.api[0].name, "")
}

output "production_ui_service" {
  description = "Cloud Run UI service receiving only load-balancer/internal ingress."
  value       = try(google_cloud_run_v2_service.ui[0].name, "")
}
