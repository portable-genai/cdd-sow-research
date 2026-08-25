# The case knowledge base the gcp profile's grounded retrieval reads.
#
# The stack granted roles/discoveryengine.editor and never created a data store, so the role
# pointed at nothing and the first grounded retrieval failed with a 501 that blames "the
# 'api_endpoint' configuration". Two separate defects behind one message: no data store existed,
# and the configured location tracked the deploy region.
#
# Discovery Engine does not serve every Cloud region. Its locations are `global`, `us` and `eu`,
# so `us-central1-discoveryengine.googleapis.com` resolves to nothing. `us` is the residency-
# preserving choice for a us-central1 deployment: United States data residency, which is exactly
# what the resourceLocations Org Policy on this project already pins (`in:us-locations`, at
# country granularity for the same reason).
resource "google_discovery_engine_data_store" "case_kb" {
  project                     = var.project_id
  location                    = var.knowledge_base_location
  data_store_id               = var.knowledge_base_data_store_id
  display_name                = "CDD case knowledge base"
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  depends_on = [google_project_service.required]
}

output "knowledge_base_data_store" {
  description = "Discovery Engine data store id backing grounded case retrieval."
  value       = google_discovery_engine_data_store.case_kb.data_store_id
}

output "knowledge_base_location" {
  description = "Discovery Engine location; NOT the deploy region, which it does not serve."
  value       = google_discovery_engine_data_store.case_kb.location
}
