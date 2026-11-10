import { qs, request } from "@/services/api";
import type { Paged } from "@/types/api";
import type {
  ApiKey,
  AttributeDefinition,
  AuditEntry,
  BulkOperation,
  BulkPreview,
  Category,
  Charts,
  CommerceProduct,
  Conflict,
  HistoryEvent,
  Integration,
  Inventory,
  InventoryTx,
  LowStockItem,
  PriceHistoryRow,
  Pricing,
  ScheduledChange,
  StockStatus,
  SyncJob,
  SyncSchedule,
  Variant,
} from "@/types/commerce";

type Q = Record<string, string | number | boolean | undefined | null>;
type BulkResult = { preview?: BulkPreview; operation?: BulkOperation };

export const commerce = {
  // products
  products: (p: Q) => request<Paged<CommerceProduct & { open_issues?: number }>>("GET", `/products${qs(p)}`),
  product: (id: string) => request<CommerceProduct & Record<string, unknown>>("GET", `/products/${id}`),
  facets: () => request<Record<"brands" | "product_types" | "lifecycle" | "publication" | "stock" | "origins", { value: string | null; count: number }[]>>("GET", "/products/facets"),
  createProduct: (b: Record<string, unknown>) => request<CommerceProduct>("POST", "/products", b),
  updateProduct: (id: string, b: Record<string, unknown>) => request<CommerceProduct>("PATCH", `/products/${id}`, b),
  archive: (id: string) => request<CommerceProduct>("POST", `/products/${id}/archive`),
  restore: (id: string) => request<CommerceProduct>("POST", `/products/${id}/restore`),
  remove: (id: string) => request<void>("DELETE", `/products/${id}`),
  history: (id: string) => request<{ items: HistoryEvent[] }>("GET", `/products/${id}/history`),

  // pricing
  pricing: (id: string) => request<Pricing>("GET", `/products/${id}/pricing`),
  setPricing: (id: string, b: Record<string, unknown>) => request<Pricing & { changed: string[] }>("PUT", `/products/${id}/pricing`, b),
  priceHistory: (id: string) => request<Paged<PriceHistoryRow>>("GET", `/products/${id}/price-history?page_size=100`),
  allPriceHistory: (p: Q = {}) => request<Paged<PriceHistoryRow>>("GET", `/pricing/history${qs(p)}`),
  pricingOverview: () => request<{ products: number; price_status: Record<string, number>; currencies: Record<string, number>; scheduled_changes: number; missing_cost: number }>("GET", "/pricing/overview"),
  schedules: (status = "scheduled") => request<Paged<ScheduledChange>>("GET", `/pricing/schedules${qs({ status, page_size: 100 })}`),
  schedule: (b: Record<string, unknown>) => request<ScheduledChange>("POST", "/pricing/schedules", b),
  cancelSchedule: (id: string) => request<ScheduledChange>("POST", `/pricing/schedules/${id}/cancel`),

  // inventory
  inventory: (id: string) => request<Inventory & { availability: string | null; variants: Variant[] }>("GET", `/products/${id}/inventory`),
  setInventory: (id: string, b: Record<string, unknown>) => request<Inventory>("PUT", `/products/${id}/inventory`, b),
  adjust: (b: { product_id: string; variant_id?: string | null; operation: string; quantity: number; reason?: string }) =>
    request<{ previous: number; new: number; adjustment: number; stock_status: StockStatus }>("POST", "/inventory/adjust", b),
  transactions: (p: Q = {}) => request<Paged<InventoryTx>>("GET", `/inventory/transactions${qs(p)}`),
  lowStock: () => request<{ items: LowStockItem[]; counts: Record<StockStatus, number>; recent_transactions: InventoryTx[] }>("GET", "/inventory/low-stock"),

  // variants
  variants: (productId: string, includeArchived = false) => request<{ items: Variant[] }>("GET", `/products/${productId}/variants${qs({ include_archived: includeArchived })}`),
  allVariants: (p: Q = {}) => request<Paged<Variant>>("GET", `/variants${qs(p)}`),
  createVariant: (productId: string, b: Record<string, unknown>) => request<Variant>("POST", `/products/${productId}/variants`, b),
  updateVariant: (id: string, b: Record<string, unknown>) => request<Variant>("PATCH", `/variants/${id}`, b),
  archiveVariant: (id: string) => request<void>("DELETE", `/variants/${id}`),

  // categories & attributes
  categories: (includeArchived = false) => request<{ items: Category[]; uncategorised: number; unmapped_texts: { category: string; count: number }[] }>("GET", `/categories${qs({ include_archived: includeArchived })}`),
  createCategory: (b: { name: string; parent_id?: string | null; description?: string }) => request<Category>("POST", "/categories", b),
  updateCategory: (id: string, b: Record<string, unknown>) => request<Category>("PATCH", `/categories/${id}`, b),
  deleteCategory: (id: string) => request<void>("DELETE", `/categories/${id}`),
  importCategories: () => request<{ created: number; linked: number }>("POST", "/categories/import-from-products"),
  definitions: () => request<{ items: AttributeDefinition[]; observed_keys: { key: string; count: number }[] }>("GET", "/attribute-definitions"),
  createDefinition: (b: Record<string, unknown>) => request<AttributeDefinition>("POST", "/attribute-definitions", b),
  updateDefinition: (id: string, b: Record<string, unknown>) => request<AttributeDefinition>("PATCH", `/attribute-definitions/${id}`, b),

  // bulk
  bulk: (b: { operation: string; product_ids: string[]; params: Record<string, unknown>; dry_run: boolean }) => request<BulkResult>("POST", "/bulk-operations", b),
  bulkOperation: (id: string) => request<BulkOperation>("GET", `/bulk-operations/${id}`),
  bulkOperations: () => request<Paged<BulkOperation>>("GET", "/bulk-operations"),

  // sync
  schedulesSync: () => request<{ items: SyncSchedule[]; extraction_methods: string[]; detect_kinds: string[] }>("GET", "/sync/schedules"),
  saveSchedule: (b: Record<string, unknown>) => request<SyncSchedule>("POST", "/sync/schedules", b),
  syncs: (p: Q = {}) => request<Paged<SyncJob> & { open_conflicts: number }>("GET", `/sync${qs(p)}`),
  sync: (id: string) => request<SyncJob>("GET", `/sync/${id}`),
  startSync: (b: { website_id?: string; integration_id?: string; direction?: string }) => request<SyncJob>("POST", "/sync", b),
  conflicts: (status = "open") => request<Paged<Conflict>>("GET", `/sync/conflicts${qs({ status, page_size: 100 })}`),
  resolveConflict: (id: string, action: string, value?: unknown) => request<Conflict>("POST", `/sync/conflicts/${id}/resolve`, { action, value }),
  resolveSync: (id: string, action: string, conflict_ids?: string[]) => request<{ resolved: number }>("POST", `/sync/${id}/resolve`, { action, conflict_ids }),

  // integrations
  integrations: () => request<{ items: Integration[]; providers: Record<string, { available: boolean; oauth_configured?: boolean; auth: string; docs: string; api_version?: string }> }>("GET", "/integrations"),
  connectWoo: (b: Record<string, unknown>) => request<Integration & { verification: { ok: boolean; message?: string }; webhook_secret?: string; webhook_setup?: string }>("POST", "/integrations/woocommerce/connect", b),
  connectShopify: (b: Record<string, unknown>) => request<Integration & { verification?: { ok: boolean; message?: string }; authorize_url?: string }>("POST", "/integrations/shopify/connect", b),
  verifyIntegration: (id: string) => request<Integration & { verification: { ok: boolean; message?: string } }>("POST", `/integrations/${id}/verify`),
  syncIntegration: (id: string, b: Record<string, unknown>) => request<SyncJob>("POST", `/integrations/${id}/sync`, b),
  disconnect: (id: string) => request<{ status: string }>("DELETE", `/integrations/${id}`),

  // API keys
  apiKeys: () => request<{ items: ApiKey[]; scopes: Record<string, string> }>("GET", "/api-keys"),
  createKey: (b: { name: string; scopes: string[]; rate_limit_per_minute?: number }) => request<ApiKey & { key: string }>("POST", "/api-keys", b),
  rotateKey: (id: string) => request<ApiKey & { key: string }>("POST", `/api-keys/${id}/rotate`),
  revokeKey: (id: string) => request<ApiKey>("POST", `/api-keys/${id}/revoke`),
  catalogRules: () => request<{ enforce_unique_skus: boolean; default_low_stock_threshold: number }>("GET", "/settings/catalog-rules"),
  setCatalogRules: (b: Record<string, unknown>) => request<{ enforce_unique_skus: boolean; default_low_stock_threshold: number }>("PATCH", "/settings/catalog-rules", b),

  // audit & dashboard
  audit: (p: Q = {}) => request<Paged<AuditEntry> & { actions: string[] }>("GET", `/audit-logs${qs(p)}`),
  charts: (days = 30) => request<Charts>("GET", `/dashboard/charts?days=${days}`),
  activity: () => request<{ items: AuditEntry[] }>("GET", "/dashboard/activity"),
};
