import { qs, request } from "@/services/api";

type Q = Record<string, string | number | boolean | undefined | null>;
export type Level = "excellent" | "good" | "needs_improvement" | "critical";
export type Priority = "critical" | "high" | "medium" | "low";

export interface DimensionResult { score: number | null; weight: number; label: string; issues: string[]; applies: boolean }
export interface QualityResult {
  product_id: string; name: string | null; sku: string | null; category: string | null; score: number; level: Level;
  dimensions: Record<string, DimensionResult>; missing_required: string[]; missing_optional: string[]; invalid: string[];
  stale: string[]; blockers: string[]; withheld: string[]; suggestions: string[]; computed_at: string;
}
export interface ProductQuality extends QualityResult {
  required_attributes: string[];
  attributes: { key: string; label: string; value: string | null; unit: string | null }[];
  agent_interest_30d: Interest | null;
  anomalies: Anomaly[];
}
export interface Snapshot { day: string; products: number; average_score: number | null; distribution_ready: number; stale_products: number; attention_products: number; dimensions: Record<string, number | null> }
export interface CategoryQuality { category: string; products: number; average: number | null; dimensions: Record<string, number | null>; missing?: { field: string; products: number }[] }
export interface QualitySummary {
  products: number; average_score: number | null; levels: Record<Level, number>; attention: number; stale: number;
  missing_specifications: number; distribution_ready: number;
  dimensions: Record<string, { label: string; average: number | null; products: number }>;
  categories: CategoryQuality[]; missing_required: { field: string; products: number }[];
}
export interface Comparison { available: boolean; reason?: string; change?: Record<string, number | null>; previous?: Snapshot; current?: Snapshot }
export interface Interest { product_id: string; name: string | null; product_detail: number; compare: number; availability: number; pricing: number; variants: number; total: number }
export interface Recommendation {
  id: string; kind: string; title: string; description: string; reason: string; evidence: Record<string, unknown> & { products?: { id: string; name: string | null }[] };
  product_ids: string[]; priority: Priority; priority_rule: string; expected_benefit: string; suggested_action: string;
  requires_approval: boolean; action: string | null; status: string; source: string; created_at: string | null;
}
export interface Anomaly {
  id: string; product_id: string | null; product_name: string | null; rule: string; rule_label: string; severity: "high" | "medium" | "low";
  title: string; detail: string; evidence: Record<string, unknown>; suggestion: string; status: string; detected_at: string | null;
}
export interface Report { id: string; kind: string; title: string; format: string; status: string; period_start: string | null; period_end: string | null; filename: string | null; size_bytes: number; coverage: { warnings?: string[] }; created_at: string }
export interface Notification { id: string; kind: string; label: string; title: string; body: string; severity: string; link: string | null; count: number; status: string; email_status: string; last_occurred_at: string }
export interface Suggestion {
  id: string; product_id: string; product_name: string | null; kind: "description" | "attributes";
  original: { description?: string | null; attributes?: { key: string; label: string; value: string | null; unit: string | null }[] };
  proposed: { description?: string; attributes?: { key: string; label: string; value: string; unit: string | null; source_snippet: string }[] };
  rationale: string | null; missing_information: string[]; source: string; model_id: string | null;
  grounding: { valid?: boolean; issues?: string[]; note?: string; rejected_model_output?: boolean }; status: string; label: string;
}
export interface AssistantAnswer {
  conversation_id: string; message_id: string; intent: string; answer: string; source: string; model_id: string | null; label: string;
  facts: Record<string, unknown> & { products?: { id: string; name: string | null; score: number; level: string; missing_required: string[] }[] };
  referenced_product_ids: string[]; next_actions: { label: string; link: string | null }[]; data_gaps: string[];
  grounding: { valid: boolean; issues: string[]; mode: string; note?: string; rejected_model_output?: boolean };
}

const I = "/intelligence";
export const intelligence = {
  overview: (days = 30) => request<Record<string, any>>("GET", `${I}/overview${qs({ days })}`),
  settings: () => request<Record<string, any>>("GET", `${I}/settings`),
  updateSettings: (b: Record<string, unknown>) => request<Record<string, any>>("PUT", `${I}/settings`, b),
  refresh: (force = false) => request<Record<string, unknown>>("POST", `${I}/refresh${qs({ force })}`),
  quality: (p: Q) => request<{ summary: QualitySummary; history: Snapshot[]; comparisons: { week: Comparison; month: Comparison };
    methodology: { formula: string; weights: Record<string, number>; dimensions: Record<string, string>; levels: Record<string, number>; note: string };
    items: QualityResult[]; total: number }>("GET", `${I}/catalog-quality${qs(p)}`),
  productQuality: (id: string) => request<ProductQuality>("GET", `${I}/products/${id}/quality`),
  categories: () => request<{ items: CategoryQuality[]; note: string; category_requirements: Record<string, string[]> }>("GET", `${I}/categories/quality`),
  searchInsights: (days: number) => request<Record<string, any>>("GET", `${I}/search-insights${qs({ days })}`),
  topProducts: (days: number) => request<{ products: Interest[]; categories: { category: string; requests: number }[]; disclaimer: string }>("GET", `${I}/top-products${qs({ days })}`),
  zeroResults: (days: number) => request<{ items: Record<string, any>[]; note: string }>("GET", `${I}/zero-result-queries${qs({ days })}`),
  recommendations: (p: Q) => request<{ items: Recommendation[]; priority_rules: Record<Priority, string> }>("GET", `${I}/recommendations${qs(p)}`),
  dismiss: (id: string) => request<Recommendation>("POST", `${I}/recommendations/${id}/dismiss`),
  reopen: (id: string) => request<Recommendation>("POST", `${I}/recommendations/${id}/reopen`),
  apply: (id: string, b: { confirm: boolean; use_ai?: boolean }) => request<Recommendation & { result: Record<string, unknown> }>("POST", `${I}/recommendations/${id}/apply`, b),
  anomalies: (p: Q) => request<{ items: Anomaly[]; rules: Record<string, string>; note: string }>("GET", `${I}/anomalies${qs(p)}`),
  dismissAnomaly: (id: string) => request<Anomaly>("POST", `${I}/anomalies/${id}/dismiss`),
  checkImages: () => request<{ broken_images: number }>("POST", `${I}/anomalies/check-images`),
  pricing: (days: number) => request<Record<string, any>>("GET", `${I}/pricing-insights${qs({ days })}`),
  inventory: (days: number) => request<Record<string, any>>("GET", `${I}/inventory-insights${qs({ days })}`),
  reports: () => request<{ items: Report[]; kinds: Record<string, string>; formats: string[] }>("GET", `${I}/reports`),
  createReport: (b: { kind: string; format: string; days: number }) => request<Report>("POST", `${I}/reports`, b),
  downloadReport: async (r: Report) => {
    const res = await request<Response>("GET", `${I}/reports/${r.id}/download`, undefined, { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = r.filename ?? `${r.kind}.${r.format}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  notifications: (status = "active") => request<{ items: Notification[]; unread: number; email: { configured: boolean; note: string } }>("GET", `${I}/notifications${qs({ status })}`),
  unread: () => request<{ unread: number }>("GET", `${I}/notifications/unread-count`),
  readNotification: (id: string) => request<Notification>("POST", `${I}/notifications/${id}/read`),
  dismissNotification: (id: string) => request<Notification>("POST", `${I}/notifications/${id}/dismiss`),
  readAll: () => request<{ updated: number }>("POST", `${I}/notifications/read-all`),
  ask: (b: { question: string; conversation_id?: string | null; use_ai?: boolean }) => request<AssistantAnswer>("POST", `${I}/assistant/query`, b),
  suggest: (b: { product_id: string; kind: "description" | "attributes"; use_ai?: boolean }) => request<Suggestion>("POST", `${I}/assistant/suggestions`, b),
  suggestions: (status = "pending") => request<{ items: Suggestion[] }>("GET", `${I}/assistant/suggestions${qs({ status })}`),
  approve: (id: string, b: { attribute_keys?: string[]; description?: string }) => request<Suggestion>("POST", `${I}/assistant/suggestions/${id}/approve`, b),
  reject: (id: string) => request<Suggestion>("POST", `${I}/assistant/suggestions/${id}/reject`),
};

export const LEVEL_TONE: Record<Level, "success" | "info" | "warning" | "danger"> = {
  excellent: "success", good: "info", needs_improvement: "warning", critical: "danger",
};
export const PRIORITY_TONE: Record<Priority, "danger" | "warning" | "info" | "neutral"> = {
  critical: "danger", high: "warning", medium: "info", low: "neutral",
};
export const SEVERITY_TONE: Record<string, "danger" | "warning" | "neutral"> = { high: "danger", medium: "warning", low: "neutral" };
// must match app/services/intelligence/quality.py LEVELS
export const LEVEL_THRESHOLDS: [Level, number][] = [["excellent", 85], ["good", 70], ["needs_improvement", 50], ["critical", 0]];
export const levelFor = (score: number): Level => (LEVEL_THRESHOLDS.find(([, low]) => score >= low) ?? ["critical", 0])[0];
// must match app/services/intelligence/reports.py KINDS
export const REPORT_KINDS = ["catalog_quality", "missing_attributes", "product_completeness", "distribution_activity", "search_performance",
  "freshness", "pricing_history", "inventory_health", "sync_conflicts", "recommendations"] as const;
