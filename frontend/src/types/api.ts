export type QualityStatus = "valid" | "needs_review" | "incomplete" | "duplicate_candidate" | "invalid";
export type ApprovalStatus = "needs_review" | "approved" | "rejected";
export type ScanStatus = "queued" | "running" | "awaiting_review" | "completed" | "failed" | "cancelled";
export type Severity = "error" | "warning" | "info";
export type ExportFormat = "json" | "csv" | "jsonld" | "feed";

export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Business {
  id: string;
  name: string;
  contact_email: string | null;
  default_currency: string | null;
  country: string | null;
  catalog_api_key_prefix: string | null;
}

export interface Me {
  id: string;
  email: string;
  full_name: string | null;
  business: Business;
}

export interface Website {
  id: string;
  url: string;
  normalized_url: string;
  domain: string;
  name: string | null;
  max_pages: number;
  max_depth: number;
  use_sitemap: boolean;
  render_js: boolean;
  last_scan_at: string | null;
  created_at: string | null;
}

export interface StageHistory {
  stage: string;
  started_at: string;
  finished_at: string | null;
}

export type ScanOutcome = ScanStatus | "completed_with_warnings";

export interface ScanError {
  stage: string;
  level?: "info" | "warning" | "error";
  message: string;
  url: string | null;
  at: string;
}

export interface ChangeSummary {
  new?: number;
  changed?: number;
  unchanged?: number;
  removed?: number;
  price_changes?: number;
  description_changes?: number;
  specification_changes?: number;
  availability_changes?: number;
  applied?: Record<string, number>;
}

export interface Scan {
  id: string;
  outcome: ScanOutcome;
  website_id: string;
  website_url: string | null;
  status: ScanStatus;
  stage: string;
  is_rescan: boolean;
  pages_discovered: number;
  pages_crawled: number;
  pages_failed: number;
  pages_skipped_robots: number;
  product_pages_found: number;
  products_extracted: number;
  ai_calls: number;
  errors: ScanError[];
  change_summary: ChangeSummary;
  stage_history: StageHistory[];
  cancel_requested: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  applied_at: string | null;
  // only on GET /scans/{id}
  ai?: { requests: number; succeeded: number; cached: number; failed: number; tokens: number; estimated_cost_usd: number; models: string[] };
  extraction_methods?: { method: string; source_type: string; products: number }[];
}

export interface ScanProgress {
  id: string;
  status: ScanStatus;
  outcome: ScanOutcome;
  website_url: string | null;
  stage: string;
  stages: { key: string; state: "done" | "active" | "pending" | "failed" }[];
  counters: Record<string, number>;
  errors: ScanError[];
  change_summary: ChangeSummary;
}

export interface FieldChange {
  field: string;
  category: string;
  old: unknown;
  new: unknown;
  manual_override: boolean;
  reason?: string;
}

export interface SnapshotChange {
  id: string;
  change_type: "new" | "changed" | "removed" | "unchanged";
  decision: "pending" | "applied" | "skipped";
  product_id: string | null;
  name: string | null;
  match_key: string;
  changes: FieldChange[];
  price: { amount: number | null; currency: string | null } | null;
  source_url: string | null;
}

export interface CrawledPage {
  id: string;
  url: string;
  title: string | null;
  status_code: number | null;
  page_type: "product" | "listing" | "other";
  product_score: number;
  signals: { reasons?: string[]; used_ai?: boolean; products?: number };
  error: string | null;
  depth: number;
  render_mode: string;
  byte_size: number;
}

export interface Attribute {
  key: string;
  label: string;
  value: string | null;
  unit: string | null;
  raw_value?: string | null;
  source?: string;
}

export interface Product {
  id: string;
  name: string | null;
  sku: string | null;
  brand: string | null;
  manufacturer: string | null;
  category: string | null;
  category_inferred: boolean;
  description: string | null;
  price: { amount: number | null; currency: string | null } | null;
  availability: string | null;
  product_url: string | null;
  source_url: string | null;
  gtin: string | null;
  mpn: string | null;
  material: string | null;
  dimensions: { value: string; unit: string | null; raw?: string } | null;
  weight: { value: string; unit: string | null; raw?: string } | null;
  images: { url: string; alt?: string | null }[];
  attributes: Attribute[];
  variants: { id: string; name: string | null; sku: string | null; price: number | null; currency: string | null; availability: string | null; attributes: Record<string, string> }[];
  quality_status: QualityStatus;
  quality_score: number;
  approval_status: ApprovalStatus;
  approved_at: string | null;
  manual_overrides: string[];
  version: number;
  is_removed: boolean;
  website_id: string | null;
  last_scan_id: string | null;
  flags: { code: string; field?: string; message?: string }[];
  created_at: string | null;
  updated_at: string | null;
  open_issues?: number;
}

export interface Evidence {
  id: string;
  field: string;
  value: unknown;
  source_value: string | null;
  source_url: string | null;
  source_fragment: string | null;
  source_type: string;
  extraction_method: "deterministic" | "ai" | "ai_mock" | "merchant" | "integration";
  inferred: boolean;
  model_id: string | null;
  prompt_version: string | null;
  confidence: number | null;
  verified_in_source: boolean | null;
  created_at: string | null;
}

export interface Issue {
  id: string;
  product_id: string;
  code: string;
  severity: Severity;
  field: string | null;
  message: string;
  details: Record<string, unknown>;
  status: "open" | "resolved" | "dismissed";
  resolution_note: string | null;
  resolved_at: string | null;
  created_at: string | null;
  product?: { id: string; name: string | null; sku: string | null; quality_status: QualityStatus; source_url: string | null };
}

export interface ProductDetail extends Product {
  evidence: Evidence[];
  issues: Issue[];
  source_page: {
    id: string;
    url: string;
    title: string | null;
    page_type: string;
    fetched_at: string;
    text_excerpt: string;
    signals: CrawledPage["signals"];
  } | null;
}

export interface ExportJob {
  id: string;
  format: ExportFormat;
  status: string;
  product_count: number;
  filters: Record<string, unknown>;
  filename: string;
  size_bytes: number;
  error: string | null;
  created_at: string | null;
}

export interface Overview {
  total_products: number;
  quality: Record<QualityStatus, number>;
  approval: Record<ApprovalStatus, number>;
  catalog_completeness: number | null;
  open_issues: Record<Severity, number>;
  categories: { category: string; count: number }[];
  latest_scan: Scan | null;
  recent_changes: { id: string; scan_id: string; change_type: string; decision: string; name: string | null; fields: string[]; at: string }[];
  recent_exports: ExportJob[];
  websites: number;
  commerce?: import("@/types/commerce").CommerceOverview;
}

export interface AISettings {
  provider: string;
  configured: boolean;
  is_mock: boolean;
  model: string | null;
  extraction_mode: string;
  max_ai_calls_per_scan: number;
  pricing_usd_per_mtok: { input: number; output: number };
  usage: { model: string; requests: number; cached: number; errors: number; prompt_tokens: number; completion_tokens: number; estimated_cost_usd: number; avg_latency_ms: number | null }[];
  tokens_per_100_products: number | null;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: Record<string, unknown> };
}
