import type { Product } from "@/types/api";

export type Lifecycle = "draft" | "active" | "archived" | "discontinued";
export type Publication = "unpublished" | "published";
export type StockStatus = "in_stock" | "low_stock" | "out_of_stock" | "backorder" | "untracked";
export type PriceStatus = "no_price" | "regular" | "on_sale" | "sale_scheduled";

export interface Pricing {
  currency: string | null;
  regular_price: string | null;
  sale_price: string | null;
  sale_starts_at: string | null;
  sale_ends_at: string | null;
  effective_price: string | null;
  price_status: PriceStatus;
  tax_class: string | null;
  tax_included: boolean | null;
  cost_price?: string | null;
  scheduled_changes?: ScheduledChange[];
}

export interface Inventory {
  track_inventory: boolean;
  stock_status: StockStatus;
  on_hand?: number;
  reserved?: number;
  available?: number;
  low_stock_threshold?: number | null;
  effective_low_stock_threshold?: number | null;
  backorder_policy?: "deny" | "allow";
  location?: string | null;
  version?: number;
}

export interface Variant {
  id: string;
  product_id: string;
  name: string | null;
  sku: string | null;
  options: Record<string, string>;
  attributes: Record<string, string>;
  price: number | null;
  regular_price: string | null;
  sale_price: string | null;
  inherits_price: boolean;
  effective_price: string | null;
  price_status: PriceStatus;
  currency: string | null;
  availability: string | null;
  image_url: string | null;
  position: number;
  is_enabled: boolean;
  origin: string;
  archived_at: string | null;
  stock_status: StockStatus;
  track_inventory: boolean;
  inventory?: { on_hand: number; reserved: number; available: number; low_stock_threshold: number | null; effective_low_stock_threshold: number | null; backorder_policy: string; location: string | null };
  product_name?: string;
  product_sku?: string | null;
}

export interface CommerceProduct extends Omit<Product, "variants"> {
  origin: string;
  product_type: string | null;
  lifecycle_status: Lifecycle;
  publication_status: Publication;
  is_public: boolean;
  published_at: string | null;
  archived_at: string | null;
  deleted_at: string | null;
  category_id: string | null;
  pricing: Pricing;
  stock_status: StockStatus;
  inventory: Inventory;
  shipping: { requires_shipping?: boolean; shipping_class?: string; lead_time_days?: number; notes?: string };
  seo_title: string | null;
  seo_description: string | null;
  variants: Variant[];
  open_conflicts?: number;
}

export interface ScheduledChange {
  id: string;
  product_id: string;
  product_name: string | null;
  variant_id: string | null;
  price_type: "regular" | "sale";
  new_amount: string | null;
  currency: string | null;
  effective_at: string;
  status: "scheduled" | "applying" | "applied" | "cancelled" | "failed";
  reason: string | null;
  error: string | null;
  applied_at: string | null;
  created_at: string | null;
}

export interface PriceHistoryRow {
  id: string;
  product_id: string;
  product_name: string | null;
  variant_id: string | null;
  price_type: "regular" | "sale" | "cost";
  old_amount: string | null;
  new_amount: string | null;
  currency: string | null;
  change_type: string;
  reason: string | null;
  actor: string | null;
  actor_type: string;
  bulk_operation_id: string | null;
  created_at: string;
}

export interface InventoryTx {
  id: string;
  product_id: string;
  product_name: string | null;
  variant_id: string | null;
  transaction_type: string;
  quantity_field: string;
  previous_quantity: number;
  adjustment: number;
  new_quantity: number;
  reason: string | null;
  actor: string | null;
  actor_type: string;
  created_at: string;
}

export interface LowStockItem {
  product_id: string;
  variant_id: string | null;
  name: string;
  sku: string | null;
  stock_status: StockStatus;
  available: number;
  on_hand: number;
  threshold: number | null;
}

export interface Category {
  id: string;
  name: string;
  path: string;
  parent_id: string | null;
  description: string | null;
  position: number;
  archived_at: string | null;
  product_count: number;
}

export interface AttributeDefinition {
  id: string;
  key: string;
  name: string;
  data_type: "string" | "number" | "boolean" | "date" | "enum";
  description: string | null;
  unit: string | null;
  is_required: boolean;
  allowed_values: string[];
  aliases: string[];
  category_id: string | null;
  archived_at: string | null;
  products_with_value?: number;
}

export interface BulkPreviewItem {
  product_id: string;
  name: string | null;
  sku?: string | null;
  status: "ok" | "invalid";
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
  error?: string;
}

export interface BulkPreview {
  operation: string;
  label: string;
  params: Record<string, unknown>;
  selected: number;
  valid: number;
  invalid: number;
  items: BulkPreviewItem[];
  run_mode: "inline" | "background";
}

export interface BulkOperation {
  id: string;
  operation: string;
  label: string;
  params: Record<string, unknown>;
  status: "queued" | "running" | "completed" | "completed_with_errors" | "failed";
  total: number;
  processed: number;
  succeeded: number;
  failed: number;
  run_mode: string;
  actor: string | null;
  created_at: string | null;
  finished_at: string | null;
  items?: { id: string; product_id: string | null; name: string | null; status: string; before: unknown; after: unknown; error: string | null }[];
}

export interface SyncSchedule {
  id: string | null;
  website_id: string;
  website_url: string | null;
  frequency: "manual" | "daily" | "weekly" | "monthly";
  enabled: boolean;
  timezone: string;
  hour: number;
  minute: number;
  weekday: number;
  day_of_month: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_scan_id: string | null;
  last_error: string | null;
  max_pages: number | null;
  max_products: number | null;
  extraction_methods: string[];
  detect: Record<string, boolean>;
  conflict_policy: "review" | "keep_agentready" | "accept_website";
  auto_apply: boolean;
}

export interface SyncJob {
  id: string;
  source_type: string;
  source_label: string | null;
  website_id: string | null;
  integration_id: string | null;
  scan_id: string | null;
  direction: "import" | "export";
  trigger: string;
  status: string;
  products_discovered: number;
  products_created: number;
  products_changed: number;
  products_ignored: number;
  conflicts_detected: number;
  failed_operations: number;
  errors: { message?: string; url?: string | null; name?: string; level?: string }[];
  summary: Record<string, unknown>;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  conflicts?: Conflict[];
}

export interface Conflict {
  id: string;
  product_id: string;
  product_name: string | null;
  field: string;
  website_value: unknown;
  agentready_value: unknown;
  original_value: unknown;
  evidence: { source_url?: string; source_value?: string; source_type?: string; value?: unknown }[];
  suggested_action: string;
  suggestion_reason: string | null;
  status: "open" | "resolved";
  resolution: string | null;
  resolved_value: unknown;
  sync_job_id: string | null;
  source_type: string;
  detected_at: string | null;
  resolved_at: string | null;
}

export interface Integration {
  id: string;
  provider: "woocommerce" | "shopify";
  name: string | null;
  store_url: string;
  status: "pending" | "connected" | "error" | "disconnected";
  credentials_hint: string | null;
  has_webhook_secret: boolean;
  settings: Record<string, unknown>;
  currency: string | null;
  last_error: string | null;
  last_verified_at: string | null;
  last_sync_at: string | null;
  webhook_path: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  rate_limit_per_minute: number;
  usage_count: number;
  last_used_at: string | null;
  revoked_at: string | null;
  expires_at: string | null;
  created_at: string | null;
  active: boolean;
  daily_usage?: { day: string; requests: number }[];
  key?: string;
}

export interface AuditEntry {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  summary: string;
  changes: Record<string, unknown>;
  actor_type: string;
  actor: string | null;
  request_id: string | null;
  operation_id: string | null;
  created_at: string;
}

export interface HistoryEvent {
  kind: "audit" | "price" | "inventory";
  at: string;
  action: string;
  summary: string;
  actor: string | null;
  actor_type: string;
  change_type?: string;
}

export interface CommerceOverview {
  active: number;
  draft: number;
  archived: number;
  discontinued: number;
  approved: number;
  published: number;
  low_stock: number;
  out_of_stock: number;
  stock: Record<StockStatus, number>;
  incomplete: number;
  pending_website_changes: number;
  pending_sync_operations: number;
  pending: { open_conflicts: number; scheduled_price_changes: number; running_syncs: number; running_bulk_operations: number };
}

export interface Charts {
  days: number;
  catalog_growth: { day: string; products: number; added: number }[];
  products_by_category: { category: string; count: number }[];
  inventory_status: { status: StockStatus; count: number }[];
  price_changes: { day: string; manual: number; bulk: number; scheduled: number; sync: number }[];
  sync_activity: { day: string; succeeded: number; failed: number; review: number }[];
}
