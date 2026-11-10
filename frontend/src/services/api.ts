import type {
  AISettings,
  Business,
  CrawledPage,
  ExportFormat,
  ExportJob,
  Issue,
  Me,
  Overview,
  Paged,
  Product,
  ProductDetail,
  Scan,
  ScanProgress,
  SnapshotChange,
  Website,
} from "@/types/api";

const BASE = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ?? "";
const PREFIX = `${BASE}/api/v1`;
const TOKEN_KEY = "ar-token";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: Record<string, unknown>) {
    super(message);
  }
}

export const tokenStore = {
  get: () => {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set: (t: string | null) => {
    try {
      if (t) sessionStorage.setItem(TOKEN_KEY, t);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable */
    }
  },
};

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function request<T>(method: string, path: string, body?: unknown, opts: { raw?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${PREFIX}${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the AgentReady API. Is the backend running?");
  }
  if (res.status === 401 && token) onUnauthorized?.();
  if (!res.ok) {
    let code = "http_error";
    let message = `Request failed (${res.status})`;
    let details: Record<string, unknown> | undefined;
    try {
      const j = await res.json();
      code = j.error?.code ?? code;
      message = j.error?.message ?? message;
      details = j.error?.details;
      const first = (details?.errors as { msg?: string; loc?: string[] }[] | undefined)?.[0];
      if (code === "validation_error" && first?.msg) message = `${first.loc?.slice(-1)[0] ?? "Field"}: ${first.msg}`;
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, code, message, details);
  }
  if (res.status === 204) return undefined as T;
  if (opts.raw) return res as unknown as T;
  return res.json() as Promise<T>;
}

export const qs = (params: Record<string, string | number | boolean | undefined | null>) => {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
};

export interface ProductQuery {
  q?: string;
  category?: string;
  quality_status?: string;
  approval_status?: string;
  website_id?: string;
  sort?: string;
  page?: number;
  page_size?: number;
}

export const api = {
  // auth
  register: (b: { email: string; password: string; full_name?: string; business_name: string }) =>
    request<{ access_token: string }>("POST", "/auth/register", b),
  login: (b: { email: string; password: string }) => request<{ access_token: string }>("POST", "/auth/login", b),
  logout: () => request<void>("POST", "/auth/logout"),
  me: () => request<Me>("GET", "/auth/me"),
  updateMe: (b: { full_name: string | null }) => request<Me>("PATCH", "/auth/me", b),
  forgotPassword: (email: string) =>
    request<{ message: string; dev_reset_url?: string }>("POST", "/auth/forgot-password", { email }),
  resetPassword: (token: string, password: string) => request<{ message: string }>("POST", "/auth/reset-password", { token, password }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ access_token: string }>("POST", "/auth/change-password", { current_password, new_password }),

  // business / settings
  updateBusiness: (b: Partial<Pick<Business, "name" | "contact_email" | "default_currency" | "country">>) =>
    request<Business>("PATCH", "/business", b),
  rotateCatalogKey: () => request<{ catalog_api_key: string; prefix: string }>("POST", "/business/catalog-key"),
  aiSettings: () => request<AISettings>("GET", "/settings/ai"),
  verifyAI: () => request<{ ok: boolean; message?: string; model?: string }>("POST", "/settings/ai/verify"),

  // websites
  validateUrl: (url: string) =>
    request<{ normalized_url: string; reachable: boolean; status_code: number | null; error: string | null; final_url: string; robots_txt_found: boolean }>(
      "POST", "/websites/validate-url", { url }),
  websites: () => request<{ items: Website[] }>("GET", "/websites"),
  createWebsite: (b: { url: string; name?: string; max_pages: number; max_depth: number; use_sitemap: boolean; render_js: boolean }) =>
    request<Website>("POST", "/websites", b),
  updateWebsite: (id: string, b: Partial<Website>) => request<Website>("PATCH", `/websites/${id}`, b),
  deleteWebsite: (id: string) => request<void>("DELETE", `/websites/${id}`),

  // scans
  startScan: (website_id: string, opts: { max_pages?: number; max_depth?: number } = {}) =>
    request<Scan>("POST", "/scans", { website_id, ...opts }),
  scans: (p: { website_id?: string; status?: string; page?: number; page_size?: number } = {}) =>
    request<Paged<Scan>>("GET", `/scans${qs(p)}`),
  scan: (id: string) => request<Scan>("GET", `/scans/${id}`),
  scanProgress: (id: string) => request<ScanProgress>("GET", `/scans/${id}/progress`),
  cancelScan: (id: string) => request<Scan>("POST", `/scans/${id}/cancel`),
  scanChanges: (id: string) => request<{ items: SnapshotChange[]; summary: Scan["change_summary"] }>("GET", `/scans/${id}/changes`),
  applyScan: (id: string, snapshot_ids?: string[]) =>
    request<{ scan: Scan; applied: Record<string, number> }>("POST", `/scans/${id}/apply`, { snapshot_ids: snapshot_ids ?? null }),
  scanPages: (id: string) => request<{ items: CrawledPage[] }>("GET", `/scans/${id}/pages`),

  // products
  products: (p: ProductQuery) => request<Paged<Product>>("GET", `/products${qs({ ...p })}`),
  categories: () => request<{ items: { category: string; count: number }[] }>("GET", "/products/categories"),
  product: (id: string) => request<ProductDetail>("GET", `/products/${id}`),
  updateProduct: (id: string, b: Record<string, unknown>) => request<Product>("PATCH", `/products/${id}`, b),
  deleteProduct: (id: string) => request<void>("DELETE", `/products/${id}`),
  bulkApproval: (product_ids: string[], approval_status: string) =>
    request<{ updated: number; skipped_blocking_issues: string[] }>("POST", "/products/bulk-approval", { product_ids, approval_status }),

  // validation
  issues: (p: { status?: string; severity?: string; code?: string; product_id?: string; page?: number; page_size?: number }) =>
    request<Paged<Issue>>("GET", `/validation/issues${qs(p)}`),
  issueSummary: () => request<{ items: { code: string; severity: string; count: number }[] }>("GET", "/validation/summary"),
  resolveIssue: (id: string, action: "resolve" | "dismiss" | "reopen", note?: string) =>
    request<Issue>("POST", `/validation/issues/${id}/resolve`, { action, note }),

  // exports
  createExport: (b: { format: ExportFormat; product_ids?: string[]; include_unapproved?: boolean; category?: string }) =>
    request<ExportJob>("POST", "/exports", b),
  exports: () => request<Paged<ExportJob>>("GET", "/exports"),
  downloadExport: async (job: ExportJob) => {
    const res = await request<Response>("GET", `/exports/${job.id}/download`, undefined, { raw: true });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = job.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },

  // dashboard
  overview: () => request<Overview>("GET", "/dashboard/overview"),
};
