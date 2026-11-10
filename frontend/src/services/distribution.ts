import { qs, request } from "@/services/api";
import type {
  AgentApiInfo,
  Analytics,
  CheckoutSession,
  DistProduct,
  DistributionOverview,
  DistributionSettings,
  Feed,
  FeedSnapshot,
  FeedToken,
  Platform,
  Preview,
  WebhookDelivery,
  WebhookSub,
} from "@/types/distribution";

type Q = Record<string, string | number | boolean | undefined | null>;
const D = "/distribution";

export const distribution = {
  settings: () => request<DistributionSettings>("GET", `${D}/settings`),
  updateSettings: (b: Record<string, unknown>) => request<DistributionSettings>("PUT", `${D}/settings`, b),
  overview: () => request<DistributionOverview>("GET", `${D}/overview`),
  products: (p: Q) => request<{ items: DistProduct[]; total: number; page: number; page_size: number; categories: { id: string; path: string; distributed: boolean }[] }>("GET", `${D}/products${qs(p)}`),
  updateProduct: (id: string, b: Record<string, unknown>) => request<DistProduct>("PATCH", `${D}/products/${id}`, b),
  bulk: (b: { product_ids: string[]; distribution_enabled?: boolean; inherit?: boolean }) => request<{ updated: number; total: number }>("POST", `${D}/products/bulk`, b),
  category: (id: string, distributed: boolean) => request<DistributionSettings>("PUT", `${D}/categories/${id}`, { distributed }),
  preview: (id: string) => request<Preview>("GET", `${D}/products/${id}/preview`),

  feeds: () => request<{ items: Feed[]; profiles: Record<string, string[]>; profile_types: Record<string, string[]>; schedules: string[] }>("GET", `${D}/feeds`),
  createFeed: (b: Record<string, unknown>) => request<Feed>("POST", `${D}/feeds`, b),
  updateFeed: (id: string, b: Record<string, unknown>) => request<Feed>("PATCH", `${D}/feeds/${id}`, b),
  archiveFeed: (id: string) => request<Feed>("DELETE", `${D}/feeds/${id}`),
  generate: (id: string) => request<FeedSnapshot>("POST", `${D}/feeds/${id}/generate`),
  snapshots: (id: string) => request<{ items: FeedSnapshot[] }>("GET", `${D}/feeds/${id}/snapshots`),
  snapshot: (id: string) => request<FeedSnapshot>("GET", `${D}/feed-snapshots/${id}`),
  downloadSnapshot: async (s: FeedSnapshot) => {
    const res = await request<Response>("GET", `${D}/feed-snapshots/${s.id}/download`, undefined, { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = s.filename ?? `feed-v${s.version}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  createToken: (feedId: string, b: Record<string, unknown>) => request<FeedToken & { url: string }>("POST", `${D}/feeds/${feedId}/tokens`, b),
  rotateToken: (id: string) => request<FeedToken & { url: string }>("POST", `${D}/feed-tokens/${id}/rotate`),
  revokeToken: (id: string) => request<FeedToken>("POST", `${D}/feed-tokens/${id}/revoke`),

  webhooks: () => request<{ items: WebhookSub[]; event_types: string[]; signing: Record<string, unknown>; retry_policy: Record<string, unknown> }>("GET", `${D}/webhooks`),
  createWebhook: (b: Record<string, unknown>) => request<WebhookSub & { secret: string }>("POST", `${D}/webhooks`, b),
  updateWebhook: (id: string, b: Record<string, unknown>) => request<WebhookSub>("PATCH", `${D}/webhooks/${id}`, b),
  disableWebhook: (id: string) => request<WebhookSub>("DELETE", `${D}/webhooks/${id}`),
  rotateSecret: (id: string) => request<WebhookSub & { secret: string }>("POST", `${D}/webhooks/${id}/rotate-secret`),
  testWebhook: (id: string) => request<WebhookDelivery>("POST", `${D}/webhooks/${id}/test`),
  deliveries: (id: string, p: Q) => request<{ items: WebhookDelivery[]; total: number }>("GET", `${D}/webhooks/${id}/deliveries${qs(p)}`),
  delivery: (id: string) => request<WebhookDelivery>("GET", `${D}/webhook-deliveries/${id}`),
  replay: (id: string) => request<WebhookDelivery>("POST", `${D}/webhook-deliveries/${id}/replay`),

  platforms: () => request<{ items: Platform[]; statuses: string[]; disclaimer: string }>("GET", `${D}/platforms`),
  updatePlatform: (p: string, b: Record<string, unknown>) => request<Platform>("PUT", `${D}/platforms/${p}`, b),
  validatePlatform: (p: string) => request<Platform>("POST", `${D}/platforms/${p}/validate`),
  application: (p: string, submitted: boolean) => request<Platform>("POST", `${D}/platforms/${p}/application`, { submitted }),
  submitPlatform: (p: string) => request<{ status: string; message: string }>("POST", `${D}/platforms/${p}/submit`),

  analytics: (p: Q) => request<Analytics>("GET", `${D}/analytics${qs(p)}`),
  agentApi: () => request<AgentApiInfo>("GET", `${D}/agent-api`),
  checkoutSessions: () => request<{ items: CheckoutSession[] }>("GET", `${D}/checkout-sessions`),
};

export const FEED_FORMATS: Record<string, string> = { json: "JSON", jsonl: "JSON Lines", csv: "CSV", jsonld: "Schema.org JSON-LD", xml: "XML" };
export const FEED_TYPES: Record<string, string> = {
  full: "Full catalog", incremental: "Incremental (changes + removals)", availability: "Availability", pricing: "Pricing",
  category: "Category", variant: "Variants",
};
export const PROFILES: Record<string, string> = { agentready: "AgentReady (canonical)", openai: "OpenAI product feed", google: "Google Merchant Center" };
// must match app/services/distribution/events.py EVENT_TYPES
export const EVENT_TYPES = [
  "product.created", "product.updated", "product.published", "product.unpublished", "product.price_changed",
  "product.availability_changed", "product.variant_updated", "product.archived",
] as const;
export const PLATFORM_STATUS_TONE: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = {
  not_configured: "neutral", setup_required: "warning", pending_verification: "info", connected: "success", syncing: "info",
  active: "success", failed: "danger", disconnected: "neutral",
};
export const DELIVERY_TONE: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = {
  pending: "warning", delivering: "info", succeeded: "success", failed: "danger",
};
