// Typed projections of the B1 domain artifacts (mirror of api/schemas.py).
// Field names match the JSON the FastAPI backend returns (enums as strings).

export type SourceType = "document" | "registry" | "media" | "regulation";

export interface Citation {
  source_id: string;
  source_type: SourceType;
  title: string;
  url?: string;
  page?: number | null;
  snippet?: string;
  score?: number | null;
  continuation_id?: string;
}

export type WealthSourceKind =
  | "employment"
  | "business_ownership"
  | "inheritance"
  | "investments"
  | "asset_sale"
  | "other";

export interface WealthSource {
  kind: WealthSourceKind;
  description: string;
  est_value_band: string;
  citations: Citation[];
}

export interface SourceOfWealthNarrative {
  subject_id: string;
  narrative: string;
  sources: WealthSource[];
  citations: Citation[];
  confidence: number;
  requires_human_review: boolean;
}

export type RiskBand = "low" | "medium" | "high" | "prohibited";

export interface RiskFactor {
  name: string;
  weight: number;
  present: boolean;
  detail: string;
  citations: Citation[];
}

export interface RiskRating {
  band: RiskBand;
  score: number;
  factors: RiskFactor[];
  rationale: string;
  citations: Citation[];
  requires_human_review: boolean;
}

export type AdverseMediaCategory =
  | "fraud"
  | "corruption"
  | "sanctions"
  | "money_laundering"
  | "terrorism"
  | "other";

export type Severity = "low" | "medium" | "high" | "critical";

export interface AdverseMediaFinding {
  headline: string;
  publisher: string;
  url: string;
  published_date?: string | null;
  category: AdverseMediaCategory;
  severity: Severity;
  snippet: string;
  citation?: Citation | null;
}

/**
 * An adverse-media screen. Its presence says a screen ran; `findings` says what it
 * returned. A bare list cannot carry both facts, so an unreachable backend and a clean
 * subject would render identically.
 */
export interface AdverseMediaScreening {
  subject_name: string;
  findings: AdverseMediaFinding[];
  sources: string[];
  searched_at: string;
}

export interface BeneficialOwner {
  name: string;
  pct: number;
  country: string;
  is_pep: boolean;
  citations: Citation[];
}

export interface OwnershipSummary {
  root_entity: string;
  owners: BeneficialOwner[];
  tree?: unknown;
  citations: Citation[];
}

export interface Subject {
  id: string;
  name: string;
  type: "individual" | "entity";
  jurisdiction: string;
  dob_or_incorp?: string | null;
}

export interface WatchlistEntry {
  uid: string;
  source: string;
  name: string;
  entity_type: string;
  aliases: string[];
  dob?: string | null;
  countries: string[];
  programs: string[];
}

export interface ScreeningAlert {
  id: string;
  status: string;
  score: number;
  matched_name: string;
  features: string[];
  entry: WatchlistEntry;
}

export interface ScreeningResult {
  query_name: string;
  lists_version: string;
  sources: string[];
  alerts: ScreeningAlert[];
  screened_at: string;
}

export interface CddCase {
  id: string;
  subject: Subject;
  sow: SourceOfWealthNarrative;
  rating: RiskRating;
  /** null/absent = not screened; empty findings = screened and clear. */
  adverse_media?: AdverseMediaScreening | null;
  ownership?: OwnershipSummary | null;
  /** null/absent = not screened; empty alerts = screened and clear. */
  screening?: ScreeningResult | null;
  requires_human_review: boolean;
  generated_at: string;
}

export interface CddRequest {
  subject: {
    id: string;
    name: string;
    type: "individual" | "entity";
    jurisdiction: string;
  };
  documents: { id: string; doc_type: string; acl_tags: string[] }[];
}

export type DocType =
  | "passport"
  | "fin_statement"
  | "registry_extract"
  | "bank_statement"
  | "other";

/** A document held in custody for a case (mirror of StoredDocumentModel). */
export interface StoredDocument {
  id: string;
  filename: string;
  doc_type: DocType;
  mime_type: string;
  size_bytes: number;
  pages: number;
  subject_id: string;
  uploaded_at: string;
  sha256: string;
  /** API-relative path serving the bytes back; resolve against the API base. */
  uri: string;
}

// --------------------------------------------------------------------------- //
// Perpetual KYC: continuous, signal-driven re-assessment and its review queue.
// Mirrors the API schemas one-for-one; every consequential figure below is computed
// server-side by deterministic code, never by a model.
// --------------------------------------------------------------------------- //
export type SignalSource = "sanctions" | "adverse_media" | "registry";
export type SignalChange = "new" | "persisting" | "cleared";
export type QueuePriority = "urgent" | "high" | "standard" | "low";

export interface MonitoringSignal {
  key: string;
  source: SignalSource;
  change: SignalChange;
  severity: Severity;
  summary: string;
  detail?: string;
  citation?: Citation | null;
  source_version?: string;
  observed_at?: string;
}

export interface SignalUplift {
  key: string;
  source: SignalSource;
  change: SignalChange;
  severity: Severity;
  uplift: number;
  reason?: string;
}

export interface ReviewQueueItem {
  id: string;
  subject_id: string;
  tenant: string;
  priority: QueuePriority;
  sla_due: string;
  reasons: string[];
  citations: Citation[];
  requires_human_review: boolean;
  routed_to_hrz7: boolean;
}

export interface PerpetualKycAssessment {
  subject_id: string;
  subject_name: string;
  tenant: string;
  as_of: string;
  signals: MonitoringSignal[];
  uplifts: SignalUplift[];
  baseline_score: number;
  baseline_band: RiskBand;
  score: number;
  score_delta: number;
  band: RiskBand;
  tier: string;
  rationale: string;
  narrative: string;
  lists_version: string;
  requires_human_review: boolean;
  queue_item: ReviewQueueItem | null;
  generated_at: string;
}

export interface PerpetualKycRequest {
  subject: {
    id: string;
    name: string;
    type: "individual" | "entity";
    jurisdiction: string;
  };
  as_of?: string;
  last_reviewed?: string;
}

// --------------------------------------------------------------------------- //
// UBO graph: the walked cross-jurisdiction ownership structure behind an entity.
// Mirrors the API schemas one-for-one. Every percentage below is the deterministic
// product of the cited registry hops, computed server-side; the model produces none of
// them, and the flags are INDICATORS, never conclusions.
// --------------------------------------------------------------------------- //
export type OwnershipNodeKind =
  | "entity"
  | "natural_person"
  | "trust"
  | "nominee"
  | "state"
  | "listed"
  | "unknown";
export type OwnershipEdgeKind =
  | "shareholding"
  | "voting"
  | "directorship"
  | "nominee_arrangement"
  | "contractual";
export type ControlBasis =
  | "effective_ownership"
  | "voting_majority"
  | "board_majority"
  | "contractual"
  | "senior_managing_official"
  | "none";
export type OwnershipFlagKind =
  | "nominee_indicator"
  | "shell_indicator"
  | "circular_holding"
  | "depth_truncated"
  | "secrecy_jurisdiction"
  | "unresolved_layer"
  | "no_owner_at_threshold";

export interface OwnershipGraphNode {
  id: string;
  name: string;
  kind: OwnershipNodeKind;
  jurisdiction: string;
  registered_address: string;
  incorporation_date: string;
  status: string;
  is_pep: boolean;
  depth: number;
  citations: Citation[];
}

export interface OwnershipEdge {
  source_id: string;
  target_id: string;
  kind: OwnershipEdgeKind;
  pct: number;
  as_of: string;
  citations: Citation[];
}

export interface OwnershipGraph {
  root_id: string;
  root_name: string;
  nodes: OwnershipGraphNode[];
  edges: OwnershipEdge[];
  depth: number;
  truncated: boolean;
  unresolved_ids: string[];
  jurisdictions: string[];
  as_of: string;
}

export interface OwnershipPathStep {
  source_id: string;
  target_id: string;
  source_name: string;
  target_name: string;
  kind: OwnershipEdgeKind;
  pct: number;
}

export interface OwnershipPath {
  steps: OwnershipPathStep[];
  product_pct: number;
  /** The multiplication rendered for a human: "60.00% x 50.00% = 30.0000%". */
  arithmetic: string;
  citations: Citation[];
}

export interface UboFinding {
  node_id: string;
  name: string;
  kind: OwnershipNodeKind;
  jurisdiction: string;
  is_pep: boolean;
  effective_pct: number;
  paths: OwnershipPath[];
  control_basis: ControlBasis;
  control_reason: string;
  meets_threshold: boolean;
  citations: Citation[];
}

export interface OwnershipFlag {
  kind: OwnershipFlagKind;
  severity: Severity;
  node_id: string;
  node_name: string;
  summary: string;
  detail: string;
  citations: Citation[];
}

export interface UboResolution {
  subject_id: string;
  subject_name: string;
  tenant: string;
  as_of: string;
  graph: OwnershipGraph | null;
  findings: UboFinding[];
  beneficial_owners: UboFinding[];
  control_basis: ControlBasis;
  control_rationale: string;
  flags: OwnershipFlag[];
  opacity_score: number;
  ownership_threshold_pct: number;
  rationale: string;
  narrative: string;
  requires_human_review: boolean;
  routed_to_hrz7: boolean;
  generated_at: string;
}

export interface UboGraphRequest {
  subject: {
    id: string;
    name: string;
    type: "individual" | "entity";
    jurisdiction: string;
  };
  as_of?: string;
}
