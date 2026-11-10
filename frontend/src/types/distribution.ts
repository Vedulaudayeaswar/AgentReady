export type Channel = "agent_api" | "mcp" | "feeds" | "webhooks";

export interface DistributionSettings {
  enabled: boolean;
  agent_api_enabled: boolean;
  mcp_enabled: boolean;
  feeds_enabled: boolean;
  webhooks_enabled: boolean;
  public_network: boolean;
  product_scope: "all_published" | "selected";
  excluded_category_ids: string[];
  show_prices: boolean;
  show_availability: boolean;
  public_attribute_keys: string[] | null;
  hidden_attribute_keys: string[];
  expires_at: string | null;
  slug: string;
  public_name: string | null;
  public_url: string | null;
  country: string | null;
  support_email: string | null;
  return_policy_url: string | null;
  privacy_policy_url: string | null;
  terms_url: string | null;
  checkout_enabled: boolean;
  channels: Channel[];
  updated_at: string | null;
}

export interface DistributionStatus {
  distributed: boolean;
  eligible: boolean;
  reasons: string[];
  channels: Record<Channel, boolean>;
  merchant_enabled: boolean;
}

export interface DistributionOverview {
  settings: DistributionSettings;
  channels: Record<Channel, boolean>;
  checklist: { key: string; label: string; done: boolean }[];
  products: { distributed: number; published: number; approved_unpublished: number; awaiting_review: number };
  feeds: { active: number; total: number; active_tokens: number; failed_7d: number };
  last_7_days: { agent_requests: number; mcp_tool_calls: number; feed_downloads: number; errors: number; checkout_sessions: number };
  webhooks: { subscriptions: number; active: number; succeeded_7d: number; failed_7d: number; pending: number };
  failed_operations_7d: number;
}

export interface DistProduct {
  id: string;
  name: string | null;
  sku: string | null;
  category: string | null;
  category_id: string | null;
  approval_status: string;
  publication_status: string;
  lifecycle_status: string;
  is_public: boolean;
  distribution_enabled: boolean | null;
  distribution_channels: Channel[] | null;
  has_public_description: boolean;
  has_public_images: boolean;
  public_attribute_keys: string[] | null;
  status: DistributionStatus;
}

export interface PublicPrice {
  currency: string | null;
  regular: string | null;
  current: string;
  sale: string | null;
  on_sale: boolean;
  sale_starts_at: string | null;
  sale_ends_at: string | null;
  upcoming_sale: { price: string; starts_at: string } | null;
  tax_included: boolean | null;
  valid_until: string | null;
}
export interface PublicAvailability { status: string; source: string | null; as_of?: string }
export interface PublicProduct {
  id: string;
  merchant: { id: string; name: string | null; url: string | null; country: string | null };
  name: string | null;
  description: string | null;
  brand: string | null;
  manufacturer: string | null;
  identifiers: { sku: string | null; gtin: string | null; mpn: string | null };
  category: { id: string | null; path: string } | null;
  product_type: string | null;
  url: string | null;
  images: string[];
  price: PublicPrice | null;
  availability: PublicAvailability | null;
  attributes: { key: string; name: string; value: string; unit: string | null; normalized: { value: number; unit: string; dimension: string } | null }[];
  variants: { id: string; name: string | null; sku: string | null; options: Record<string, string>; image: string | null; price: PublicPrice | null; availability: PublicAvailability | null }[];
  specifications?: { weight: unknown; dimensions: unknown; material: string | null };
  shipping?: Record<string, unknown> | null;
  unknown_fields: string[];
  withheld_fields: string[];
  version: number;
  updated_at: string | null;
}

export interface Preview {
  status: DistributionStatus;
  representation: PublicProduct;
  visible_to_agents: boolean;
  note: string;
  controls: { distribution_enabled: boolean | null; distribution_channels: Channel[] | null; public_description: string | null;
    public_images: string[] | null; public_attribute_keys: string[] | null };
}

export interface FeedSnapshot {
  id: string;
  feed_id: string;
  version: number;
  status: "queued" | "running" | "completed" | "failed";
  trigger: string;
  total: number;
  processed: number;
  record_count: number;
  product_count: number;
  removed_count: number;
  excluded_count: number;
  size_bytes: number;
  checksum: string | null;
  filename: string | null;
  validation: { valid?: boolean; errors?: string[]; warnings?: string[]; error_count?: number; warning_count?: number };
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
  since: string | null;
  expired: boolean;
}
export interface FeedToken {
  id: string;
  feed_id: string;
  name: string;
  prefix: string;
  url_hint: string;
  url?: string;
  rate_limit_per_minute: number;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  usage_count: number;
  active: boolean;
  created_at: string | null;
}
export interface Feed {
  id: string;
  name: string;
  profile: "agentready" | "openai" | "google";
  format: "json" | "jsonl" | "csv" | "jsonld" | "xml";
  feed_type: "full" | "incremental" | "availability" | "pricing" | "category" | "variant";
  category_id: string | null;
  schedule: "manual" | "hourly" | "daily" | "weekly";
  max_age_hours: number;
  next_run_at: string | null;
  last_generated_at: string | null;
  archived_at: string | null;
  latest: FeedSnapshot | null;
  served?: FeedSnapshot | null;
  tokens: FeedToken[];
  created_at: string | null;
}

export interface WebhookSub {
  id: string;
  url: string;
  description: string | null;
  events: string[];
  is_active: boolean;
  secret_hint: string;
  rotation_grace_until: string | null;
  consecutive_failures: number;
  last_delivery_at: string | null;
  last_success_at: string | null;
  created_at: string | null;
  secret?: string;
}
export interface WebhookDelivery {
  id: string;
  subscription_id: string;
  event_id: string;
  event_type: string;
  product_id: string | null;
  status: "pending" | "delivering" | "succeeded" | "failed";
  attempts: number;
  max_attempts: number;
  next_attempt_at: string | null;
  last_status_code: number | null;
  last_error: string | null;
  last_response_ms: number | null;
  delivered_at: string | null;
  replay_of_id: string | null;
  replay_number: number;
  attempt_log: { at: string; status: number | null; error: string | null; ms: number }[];
  created_at: string | null;
  payload?: unknown;
}

export interface Platform {
  id: string;
  provider: "openai_chatgpt" | "google_ucp" | "perplexity" | "mcp_clients";
  name: string;
  status: string;
  enabled: boolean;
  config: Record<string, unknown>;
  feed_id: string | null;
  feed_profile: string | null;
  feed_format: string | null;
  missing_setup: string[];
  onboarding: { step: string; done: boolean; blocked?: boolean }[];
  application_submitted_at: string | null;
  last_validated_at: string | null;
  last_validation: { valid?: boolean; snapshot_id?: string; records?: number; excluded?: number; errors?: string[]; warnings?: string[]; error?: string | null };
  last_sync_at: string | null;
  last_error: string | null;
  capabilities: {
    discovery: boolean; feed_formats: string[]; delivery: string; checkout: string; regions: string; submission_api: string;
    live_status: "available" | "blocked" | "not_available"; requirements: string[]; docs: string[]; blocked_reason: string | null;
  };
  history: { id: string; action: string; status: string; products: number; valid: number; invalid: number; message: string | null; at: string | null }[];
}

export interface Analytics {
  period_days: number;
  since: string;
  observed: {
    agent_requests: number;
    by_kind: Record<string, number>;
    search_requests: number;
    product_detail_requests: number;
    mcp_tool_calls: number;
    feed_downloads: number;
    checkout_sessions: number;
    errors: number;
    avg_latency_ms: number | null;
    daily: ({ day: string } & Record<string, number | string>)[];
    top_products: { product_id: string; name: string | null; requests: number }[];
    top_categories: { category: string; requests: number }[];
    consumers: { type: string; id: string | null; label: string; requests: number; last_seen: string | null }[];
    recent_errors: { at: string; kind: string; channel: string; status: number | null; message: string | null }[];
    feeds: { generated: number; failed: number; total: number; success_rate: number | null };
    webhooks: { succeeded: number; failed: number; total: number; success_rate: number | null };
  };
  derived: { freshness: { distributed_products: number; oldest_update: string | null; newest_update: string | null; oldest_age_hours: number | null } };
  not_measured: string[];
  note: string;
}

export interface AgentApiInfo {
  capabilities: {
    service: { name: string; api_version: string; product_schema: string };
    endpoints: Record<string, string>;
    tools: { name: string; title: string; description: string }[];
    mcp: { endpoint: string; transport: string; protocol_versions: string[] };
    rate_limits: Record<string, string | number>;
    data_policy: string[];
  };
  merchant_id: string;
  channels: Record<"agent_api" | "mcp", boolean>;
  keys: { id: string; name: string; prefix: string; scopes: string[]; active: boolean; agent_access: boolean; agent_requests_30d: number; total_requests: number; last_used_at: string | null; rate_limit_per_minute: number }[];
  examples: { search: string; product: string; compare: string; mcp_config: unknown };
}

export interface CheckoutSession {
  id: string;
  status: string;
  method: string;
  checkout_url: string | null;
  currency: string | null;
  items: { name: string | null; quantity: number; unit_price: string; line_total: string }[];
  subtotal: string | null;
  payment_status: string;
  order_status: string;
  created_at: string | null;
}
