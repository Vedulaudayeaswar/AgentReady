import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { BarChart3, CheckCircle2, Circle, Copy, Eye, KeyRound, Search, Share2, ShieldOff } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader, Pagination, Stat } from "@/components/app/common";
import { ConfirmDialog } from "@/components/app/commerce";
import { CHANNEL_LABELS, DistStatusBadge, ProductDistributionPanel } from "@/components/app/distribution";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, NativeSelect, Skeleton, Switch, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/overlays";
import { commerce } from "@/services/commerce";
import { distribution } from "@/services/distribution";
import type { Channel, DistributionSettings } from "@/types/distribution";
import { formatDate, humanize, timeAgo } from "@/utils/format";

const axis = { fontSize: 11, fill: "hsl(var(--muted-foreground))" };
const tip = { background: "hsl(var(--popover))", border: "1px solid hsl(var(--border))", borderRadius: 6, fontSize: 12 };
const CH_COLORS: Record<string, string> = {
  agent_api: "hsl(var(--foreground))", mcp: "hsl(var(--info))", feeds: "hsl(var(--success))", webhooks: "hsl(var(--warning))",
  checkout: "hsl(var(--muted-foreground))",
};
export const copyText = (t: string) => navigator.clipboard?.writeText(t).then(() => toast.success("Copied"), () => toast.error("Copy failed"));

/* ================================================================ Overview */
export function DistributionOverviewPage() {
  const qc = useQueryClient();
  const ov = useQuery({ queryKey: ["dist-overview"], queryFn: distribution.overview });
  const [busy, setBusy] = useState(false);
  if (ov.isLoading) return <><PageHeader title="Distribution" /><Skeleton className="h-64" /></>;
  if (ov.error || !ov.data) return <><PageHeader title="Distribution" /><ErrorNote error={ov.error} /></>;
  const d = ov.data;
  const toggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      await distribution.updateSettings({ enabled });
      toast.success(enabled ? "Public distribution is on" : "Public distribution is off");
      void qc.invalidateQueries({ queryKey: ["dist-overview"] });
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <>
      <PageHeader title="Distribution" description="Make your approved products available to AI agents, feeds and partner systems — on your terms."
        actions={<label className="flex items-center gap-2 text-[13px] font-medium"><Switch aria-label="Public distribution" checked={d.settings.enabled} disabled={busy} onCheckedChange={toggle} /> Public distribution {d.settings.enabled ? "on" : "off"}</label>} />
      {!d.settings.enabled && (
        <Card className="mb-4 border-warning/40"><CardContent className="pt-5 text-[13px]">
          Distribution is <b>off</b>: no agent, feed URL or webhook receives any product data. Preview what agents would see in <Link className="underline" to="/app/distribution/products">Public products</Link> before turning it on.
        </CardContent></Card>
      )}
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Distributed products" value={d.products.distributed} hint={`of ${d.products.published} published`} />
        <Stat label="Awaiting publication" value={d.products.approved_unpublished + d.products.awaiting_review} hint={`${d.products.awaiting_review} need review · ${d.products.approved_unpublished} approved, unpublished`} />
        <Stat label="Active feeds" value={d.feeds.active} hint={`${d.feeds.active_tokens} feed URLs`} />
        <Stat label="Agent API requests (7 days)" value={d.last_7_days.agent_requests} hint={`${d.last_7_days.mcp_tool_calls} MCP tool calls`} />
        <Stat label="Feed downloads (7 days)" value={d.last_7_days.feed_downloads} />
        <Stat label="Webhooks" value={`${d.webhooks.active} active`} hint={`${d.webhooks.succeeded_7d} delivered · ${d.webhooks.failed_7d} failed · ${d.webhooks.pending} pending`} />
        <Stat label="Failed operations (7 days)" value={d.failed_operations_7d} hint="feed generation, webhook deliveries, agent errors" />
        <Stat label="Checkout hand-offs (7 days)" value={d.last_7_days.checkout_sessions} hint="payment is never handled by AgentReady" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><div><CardTitle>Setup</CardTitle><CardDescription>Steps that make your catalog agent-ready.</CardDescription></div></CardHeader>
          <CardContent className="space-y-2 text-[13px]">
            {d.checklist.map((c) => (
              <div key={c.key} className="flex items-center gap-2">
                {c.done ? <CheckCircle2 className="h-4 w-4 text-success" /> : <Circle className="h-4 w-4 text-muted-foreground" />}
                <span className={c.done ? "text-muted-foreground line-through" : ""}>{c.label}</span>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><CardTitle>Channels</CardTitle><CardDescription>Each channel can be switched off independently in Public products → Settings.</CardDescription></div></CardHeader>
          <CardContent className="grid grid-cols-2 gap-2 text-[13px]">
            {(Object.keys(CHANNEL_LABELS) as Channel[]).map((c) => (
              <div key={c} className="flex items-center justify-between rounded border px-3 py-2">
                <span>{CHANNEL_LABELS[c]}</span>{d.channels[c] ? <Badge tone="success" dot>Open</Badge> : <Badge dot>Closed</Badge>}
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
      <p className="mt-4 text-[12px] text-muted-foreground">All figures are counted from AgentReady's own records. AgentReady cannot see whether an AI platform recommended or sold a product, so no such figures are shown.</p>
    </>
  );
}

/* ================================================================ Public products (settings + product controls) */
function SettingsCard({ s, categories, onSaved }: { s: DistributionSettings; categories: { id: string; path: string }[]; onSaved: () => void }) {
  const [f, setF] = useState(s);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => setF(s), [s]);
  const set = <K extends keyof DistributionSettings>(k: K, v: DistributionSettings[K]) => setF({ ...f, [k]: v });
  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const body: Record<string, unknown> = {};
      for (const k of ["enabled", "agent_api_enabled", "mcp_enabled", "feeds_enabled", "webhooks_enabled", "public_network", "product_scope",
        "show_prices", "show_availability", "checkout_enabled", "slug"] as const) body[k] = f[k];
      for (const k of ["public_name", "public_url", "country", "support_email", "return_policy_url", "privacy_policy_url", "terms_url"] as const)
        if (f[k]) body[k] = f[k];
      body.hidden_attribute_keys = f.hidden_attribute_keys;
      if (f.public_attribute_keys === null) body.reset_public_attribute_keys = true; else body.public_attribute_keys = f.public_attribute_keys;
      if (f.expires_at) body.expires_at = new Date(f.expires_at).toISOString(); else body.clear_expiry = true;
      await distribution.updateSettings(body);
      toast.success("Distribution settings saved");
      onSaved();
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const sw = (k: "enabled" | "agent_api_enabled" | "mcp_enabled" | "feeds_enabled" | "webhooks_enabled" | "public_network" | "show_prices" | "show_availability" | "checkout_enabled", label: string, hint?: string) => (
    <label className="flex items-start justify-between gap-3 rounded border px-3 py-2">
      <span><span className="font-medium">{label}</span>{hint && <span className="block text-[12px] text-muted-foreground">{hint}</span>}</span>
      <Switch aria-label={label} checked={f[k]} onCheckedChange={(v) => set(k, v)} />
    </label>
  );
  return (
    <Card className="mb-6">
      <CardHeader><div><CardTitle>Distribution settings</CardTitle><CardDescription>Enforced by the server for every channel — not just hidden in the UI.</CardDescription></div></CardHeader>
      <CardContent className="space-y-4 text-[13px]">
        <ErrorNote error={err} />
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {sw("enabled", "Public distribution", "Master switch for all channels")}
          {sw("agent_api_enabled", "Agent API")}
          {sw("mcp_enabled", "MCP tools")}
          {sw("feeds_enabled", "Feeds")}
          {sw("webhooks_enabled", "Webhooks")}
          {sw("public_network", "Public network", "Anonymous agents may discover you without an API key")}
          {sw("show_prices", "Share prices")}
          {sw("show_availability", "Share availability", "Stock quantities are never shared")}
          {sw("checkout_enabled", "Checkout hand-offs", "Agents may create carts that send shoppers to your checkout")}
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <Field label="Which products" htmlFor="ds-scope">
            <NativeSelect id="ds-scope" value={f.product_scope} onChange={(e) => set("product_scope", e.target.value as DistributionSettings["product_scope"])}>
              <option value="all_published">All approved & published products</option>
              <option value="selected">Only products I select</option>
            </NativeSelect>
          </Field>
          <Field label="Public store name" htmlFor="ds-name"><Input id="ds-name" value={f.public_name ?? ""} onChange={(e) => set("public_name", e.target.value)} /></Field>
          <Field label="Public merchant id" htmlFor="ds-slug" hint="Used by agents to filter by merchant"><Input id="ds-slug" value={f.slug} onChange={(e) => set("slug", e.target.value)} /></Field>
          <Field label="Website" htmlFor="ds-url"><Input id="ds-url" placeholder="https://" value={f.public_url ?? ""} onChange={(e) => set("public_url", e.target.value)} /></Field>
          <Field label="Country (ISO code)" htmlFor="ds-country"><Input id="ds-country" maxLength={2} value={f.country ?? ""} onChange={(e) => set("country", e.target.value.toUpperCase())} /></Field>
          <Field label="Support email" htmlFor="ds-email"><Input id="ds-email" value={f.support_email ?? ""} onChange={(e) => set("support_email", e.target.value)} /></Field>
          <Field label="Return policy URL" htmlFor="ds-ret"><Input id="ds-ret" value={f.return_policy_url ?? ""} onChange={(e) => set("return_policy_url", e.target.value)} /></Field>
          <Field label="Privacy policy URL" htmlFor="ds-priv"><Input id="ds-priv" value={f.privacy_policy_url ?? ""} onChange={(e) => set("privacy_policy_url", e.target.value)} /></Field>
          <Field label="Terms URL" htmlFor="ds-tos"><Input id="ds-tos" value={f.terms_url ?? ""} onChange={(e) => set("terms_url", e.target.value)} /></Field>
          <Field label="Distribute until (optional)" htmlFor="ds-exp"><Input id="ds-exp" type="datetime-local" value={f.expires_at ? f.expires_at.slice(0, 16) : ""} onChange={(e) => set("expires_at", e.target.value || null)} /></Field>
          <Field label="Public attributes" htmlFor="ds-attrs" hint="Comma-separated keys; empty = all attributes">
            <Input id="ds-attrs" value={(f.public_attribute_keys ?? []).join(", ")} onChange={(e) => set("public_attribute_keys", e.target.value.trim() ? e.target.value.split(",").map((x) => x.trim()) : null)} />
          </Field>
          <Field label="Hidden attributes" htmlFor="ds-hidden" hint="Never shared, never searchable">
            <Input id="ds-hidden" value={f.hidden_attribute_keys.join(", ")} onChange={(e) => set("hidden_attribute_keys", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
          </Field>
        </div>
        {categories.length > 0 && (
          <Field label="Categories not distributed (with their subcategories)">
            <div className="flex flex-wrap gap-3">
              {categories.map((c) => (
                <label key={c.id} className="flex items-center gap-1.5">
                  <Checkbox checked={f.excluded_category_ids.includes(c.id)} onCheckedChange={async (v) => {
                    try { await distribution.category(c.id, !v); onSaved(); } catch (e) { toast.error((e as Error).message); }
                  }} /> {c.path}
                </label>
              ))}
            </div>
          </Field>
        )}
        <Button onClick={save} loading={busy}>Save settings</Button>
      </CardContent>
    </Card>
  );
}

export function PublicProductsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["dist-settings"], queryFn: distribution.settings });
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [sel, setSel] = useState<string[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["dist-products", q, status, page], queryFn: () => distribution.products({ q, status, page, page_size: 25 }), placeholderData: keepPreviousData });
  const refresh = () => {
    for (const k of ["dist-products", "dist-settings", "dist-overview", "dist-preview"]) void qc.invalidateQueries({ queryKey: [k] });
  };
  const bulk = async (b: { distribution_enabled?: boolean; inherit?: boolean }, msg: string) => {
    try { const r = await distribution.bulk({ product_ids: sel, ...b }); toast.success(`${msg} (${r.updated} changed)`); setSel([]); refresh(); }
    catch (e) { toast.error((e as Error).message); }
  };
  const items = list.data?.items ?? [];
  return (
    <>
      <PageHeader title="Public products" description="Choose what external agents can see. Only approved and published products are ever distributed." />
      {settings.data && <SettingsCard s={settings.data} categories={list.data?.categories ?? []} onSaved={refresh} />}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input className="pl-8" aria-label="Search products" placeholder="Search name or SKU" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        </div>
        <NativeSelect aria-label="Distribution status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
          <option value="">All products</option><option value="distributed">Distributed</option><option value="not_distributed">Not distributed</option>
        </NativeSelect>
        {sel.length > 0 && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => bulk({ distribution_enabled: true }, "Distribution enabled")}>Distribute {sel.length}</Button>
            <Button size="sm" variant="outline" onClick={() => bulk({ distribution_enabled: false }, "Distribution disabled")}>Do not distribute</Button>
            <Button size="sm" variant="ghost" onClick={() => bulk({ inherit: true }, "Following store settings")}>Follow store settings</Button>
          </div>
        )}
      </div>
      <ErrorNote error={list.error} />
      <Card className="overflow-hidden">
        {list.isLoading ? <div className="p-4"><Skeleton className="h-40" /></div> : !items.length ? (
          <div className="p-6"><EmptyState icon={<Share2 />} title="No products" description="Approve and publish products in the catalog first." action={<Button asChild size="sm" variant="outline"><Link to="/app/catalog">Open catalog</Link></Button>} /></div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead><TR>
                  <TH className="w-8"><Checkbox aria-label="Select all" checked={sel.length === items.length} onCheckedChange={(v) => setSel(v ? items.map((i) => i.id) : [])} /></TH>
                  <TH>Product</TH><TH>Catalog status</TH><TH>Setting</TH><TH>Distribution</TH><TH>Channels</TH><TH />
                </TR></THead>
                <TBody>
                  {items.map((p) => (
                    <TR key={p.id}>
                      <TD><Checkbox aria-label={`Select ${p.name}`} checked={sel.includes(p.id)} onCheckedChange={(v) => setSel(v ? [...sel, p.id] : sel.filter((x) => x !== p.id))} /></TD>
                      <TD className="max-w-[280px]"><Link to={`/app/catalog/${p.id}`} className="font-medium hover:underline">{p.name}</Link><p className="text-[11.5px] text-muted-foreground">{p.sku ?? "No SKU"}{p.category ? ` · ${p.category}` : ""}</p></TD>
                      <TD className="whitespace-nowrap text-[12px]">{humanize(p.approval_status)} · {humanize(p.publication_status)}</TD>
                      <TD className="text-[12px]">{p.distribution_enabled === null ? "Store setting" : p.distribution_enabled ? "On" : "Off"}</TD>
                      <TD><DistStatusBadge status={p.status} />{p.status.reasons[0] && <p className="mt-0.5 max-w-[220px] text-[11px] text-muted-foreground">{p.status.reasons[0]}</p>}</TD>
                      <TD><div className="flex flex-wrap gap-1">{(Object.keys(CHANNEL_LABELS) as Channel[]).filter((c) => p.status.channels[c]).map((c) => <Badge key={c}>{CHANNEL_LABELS[c]}</Badge>)}</div></TD>
                      <TD className="text-right"><Button size="xs" variant="outline" onClick={() => setOpen(p.id)}><Eye /> Preview</Button></TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <div className="border-t"><Pagination page={page} pageSize={25} total={list.data!.total} onPage={setPage} /></div>
          </>
        )}
      </Card>
      <Dialog open={!!open} onOpenChange={(o) => { if (!o) { setOpen(null); refresh(); } }}>
        <DialogContent className="max-h-[90vh] max-w-5xl overflow-y-auto">
          <DialogHeader><DialogTitle>Distribution & public preview</DialogTitle></DialogHeader>
          {open && <ProductDistributionPanel productId={open} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

/* ================================================================ Agent API */
export function AgentApiPage() {
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["agent-api"], queryFn: distribution.agentApi });
  const [name, setName] = useState("");
  const [checkout, setCheckout] = useState(false);
  const [shown, setShown] = useState<string | null>(null);
  const [revoke, setRevoke] = useState<string | null>(null);
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["agent-api"] }); void qc.invalidateQueries({ queryKey: ["api-keys"] }); };
  if (info.isLoading) return <><PageHeader title="Agent API" /><Skeleton className="h-64" /></>;
  if (!info.data) return <><PageHeader title="Agent API" /><ErrorNote error={info.error} /></>;
  const d = info.data;
  const cap = d.capabilities;
  const mcpJson = JSON.stringify(d.examples.mcp_config, null, 2);
  return (
    <>
      <PageHeader title="Agent API" description={`Version ${cap.service.api_version} · schema ${cap.service.product_schema} · your merchant id: ${d.merchant_id}`} />
      {shown && (
        <Card className="mb-4 border-warning/40"><CardContent className="space-y-2 pt-5 text-[13px]">
          <p className="font-medium">Copy your new agent key now — it is stored hashed and cannot be shown again.</p>
          <div className="flex gap-2"><pre className="flex-1 overflow-x-auto rounded bg-muted p-2 font-mono text-[12px]" data-testid="new-agent-key">{shown}</pre><Button size="sm" variant="outline" onClick={() => copyText(shown)}><Copy /> Copy</Button></div>
          <Button size="xs" variant="ghost" onClick={() => setShown(null)}>I've saved it</Button>
        </CardContent></Card>
      )}
      <div className="mb-4 flex flex-wrap gap-2 text-[13px]">
        <Badge tone={d.channels.agent_api ? "success" : "neutral"} dot>Agent API {d.channels.agent_api ? "open" : "closed"}</Badge>
        <Badge tone={d.channels.mcp ? "success" : "neutral"} dot>MCP {d.channels.mcp ? "open" : "closed"}</Badge>
        {!d.channels.agent_api && <span className="text-muted-foreground">Turn on distribution in <Link className="underline" to="/app/distribution/products">Public products</Link>.</span>}
      </div>
      <Card className="mb-6 overflow-hidden">
        <CardHeader><div><CardTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4" /> Agent keys</CardTitle>
          <CardDescription>Give each agent or partner its own key so you can see its usage and revoke it independently.</CardDescription></div></CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3 border-b pb-4">
          <Field label="Key name" htmlFor="ak-name"><Input id="ak-name" placeholder="e.g. Shopping assistant" value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <label className="flex items-center gap-1.5 pb-2 text-[12.5px]"><Checkbox checked={checkout} onCheckedChange={(v) => setCheckout(!!v)} aria-label="Allow checkout hand-offs" /> Allow checkout hand-offs</label>
          <Button size="sm" disabled={!name.trim()} onClick={async () => {
            try {
              const k = await commerce.createKey({ name: name.trim(), scopes: ["agent:read", ...(checkout ? ["checkout:write"] : [])] });
              setShown(k.key); setName(""); refresh();
            } catch (e) { toast.error((e as Error).message); }
          }}>Create agent key</Button>
        </CardContent>
        {!d.keys.length ? <CardContent><p className="pt-3 text-[13px] text-muted-foreground">No keys yet.</p></CardContent> : (
          <div className="overflow-x-auto">
            <Table>
              <THead><TR><TH>Name</TH><TH>Key</TH><TH>Scopes</TH><TH className="text-right">Agent requests (30 d)</TH><TH>Rate limit</TH><TH>Last used</TH><TH /></TR></THead>
              <TBody>
                {d.keys.map((k) => (
                  <TR key={k.id} className={k.active ? "" : "opacity-60"}>
                    <TD className="font-medium">{k.name}</TD>
                    <TD className="font-mono text-[12px]">{k.prefix}…</TD>
                    <TD><div className="flex flex-wrap gap-1">{k.scopes.map((s) => <Badge key={s} tone={s === "agent:read" ? "info" : "neutral"}>{s}</Badge>)}</div>{!k.agent_access && <p className="text-[11px] text-muted-foreground">No agent access</p>}</TD>
                    <TD className="tabular text-right">{k.agent_requests_30d}</TD>
                    <TD className="text-[12px]">{k.rate_limit_per_minute}/min</TD>
                    <TD className="text-muted-foreground">{timeAgo(k.last_used_at)}</TD>
                    <TD className="text-right">{k.active ? <Button size="xs" variant="ghost" onClick={() => setRevoke(k.id)}><ShieldOff /> Revoke</Button> : <Badge tone="danger">Revoked</Badge>}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </Card>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><div><CardTitle>Endpoints</CardTitle><CardDescription>Send the key as <code>X-Catalog-Key</code> or <code>Authorization: Bearer</code>.</CardDescription></div></CardHeader>
          <CardContent className="space-y-1.5 text-[12px]">
            {Object.entries(cap.endpoints).map(([k, v]) => (
              <div key={k} className="grid gap-1 sm:grid-cols-[110px_1fr]"><span className="font-medium">{humanize(k)}</span><code className="break-all font-mono text-muted-foreground">{v}</code></div>
            ))}
            <div className="pt-3 space-y-2">
              {(["search", "product", "compare"] as const).map((k) => (
                <div key={k}><p className="mb-1 font-medium">Example: {k}</p>
                  <div className="flex gap-2"><pre className="flex-1 overflow-x-auto rounded bg-muted p-2 font-mono text-[11px]">{d.examples[k]}</pre><Button size="xs" variant="outline" onClick={() => copyText(d.examples[k])}><Copy /></Button></div></div>
              ))}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><CardTitle>Connect an MCP client</CardTitle><CardDescription>{cap.mcp.transport} · protocol {cap.mcp.protocol_versions.join(", ")}</CardDescription></div></CardHeader>
          <CardContent className="space-y-3 text-[12.5px]">
            <p>Endpoint: <code className="break-all font-mono" data-testid="mcp-endpoint">{cap.mcp.endpoint}</code></p>
            <p className="text-muted-foreground">Add this to an MCP-compatible client's configuration (replace the key variable with an agent key):</p>
            <div className="flex gap-2"><pre className="flex-1 overflow-x-auto rounded bg-muted p-2 font-mono text-[11px]">{mcpJson}</pre><Button size="xs" variant="outline" onClick={() => copyText(mcpJson)}><Copy /></Button></div>
            <p className="font-medium">Read-only tools</p>
            <ul className="space-y-1">{cap.tools.map((t) => <li key={t.name}><code className="font-mono">{t.name}</code> — <span className="text-muted-foreground">{t.description}</span></li>)}</ul>
            <p className="font-medium">Rate limits</p>
            <ul className="text-muted-foreground">{Object.entries(cap.rate_limits).map(([k, v]) => <li key={k}>{humanize(k)}: {v}</li>)}</ul>
          </CardContent>
        </Card>
      </div>
      <Card className="mt-4"><CardHeader><div><CardTitle>Data policy</CardTitle></div></CardHeader>
        <CardContent><ul className="list-disc space-y-1 pl-5 text-[12.5px] text-muted-foreground">{cap.data_policy.map((p) => <li key={p}>{p}</li>)}</ul></CardContent></Card>
      <ConfirmDialog open={!!revoke} onOpenChange={(o) => !o && setRevoke(null)} title="Revoke this agent key?" destructive confirmLabel="Revoke"
        description="The agent using it loses access immediately." onConfirm={async () => { await commerce.revokeKey(revoke!); toast.success("Key revoked"); refresh(); }} />
    </>
  );
}

/* ================================================================ Analytics */
export function DistributionAnalyticsPage() {
  const [days, setDays] = useState(30);
  const [channel, setChannel] = useState("");
  const [consumer, setConsumer] = useState("");
  const a = useQuery({ queryKey: ["dist-analytics", days, channel, consumer], queryFn: () => distribution.analytics({ days, channel, consumer }), placeholderData: keepPreviousData });
  const o = a.data?.observed;
  const channels = Array.from(new Set((o?.daily ?? []).flatMap((d) => Object.keys(d).filter((k) => k !== "day"))));
  const pct = (r: number | null | undefined) => (r == null ? "—" : `${Math.round(r * 100)}%`);
  return (
    <>
      <PageHeader title="Distribution analytics" description="Observed events only: requests AgentReady served, feeds it delivered and webhooks it sent." />
      <div className="mb-4 flex flex-wrap gap-2">
        <NativeSelect aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
        </NativeSelect>
        <NativeSelect aria-label="Channel" value={channel} onChange={(e) => setChannel(e.target.value)}>
          <option value="">All channels</option><option value="agent_api">Agent API</option><option value="mcp">MCP</option><option value="feeds">Feeds</option><option value="webhooks">Webhooks</option><option value="checkout">Checkout</option>
        </NativeSelect>
        <NativeSelect aria-label="Consumer" value={consumer} onChange={(e) => setConsumer(e.target.value)}>
          <option value="">All consumers</option>
          {(o?.consumers ?? []).filter((c) => c.id || c.type === "anonymous").map((c) => <option key={`${c.type}${c.id}`} value={c.id ?? "anonymous"}>{c.label}</option>)}
        </NativeSelect>
      </div>
      <ErrorNote error={a.error} />
      {!o ? <Skeleton className="h-64" /> : (
        <>
          <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="analytics-stats">
            <Stat label="Agent requests" value={o.agent_requests} hint={o.avg_latency_ms != null ? `avg ${o.avg_latency_ms} ms` : undefined} />
            <Stat label="Searches" value={o.search_requests} hint={`${o.product_detail_requests} product detail requests`} />
            <Stat label="MCP tool calls" value={o.mcp_tool_calls} />
            <Stat label="Feed downloads" value={o.feed_downloads} />
            <Stat label="Feed generation success" value={pct(o.feeds.success_rate)} hint={`${o.feeds.generated} ok · ${o.feeds.failed} failed`} />
            <Stat label="Webhook delivery success" value={pct(o.webhooks.success_rate)} hint={`${o.webhooks.succeeded} delivered · ${o.webhooks.failed} failed`} />
            <Stat label="Errors" value={o.errors} />
            <Stat label="Data freshness" value={a.data!.derived.freshness.oldest_age_hours != null ? `${a.data!.derived.freshness.oldest_age_hours} h` : "—"} hint="age of the oldest distributed product update" />
          </div>
          <Card className="mb-4">
            <CardHeader><div><CardTitle>Events per day</CardTitle><CardDescription>By channel</CardDescription></div></CardHeader>
            <CardContent className="h-[240px]">
              {!o.daily.length ? <EmptyState icon={<BarChart3 />} title="No events in this period" /> : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={o.daily} margin={{ left: -16, right: 8, top: 4 }}>
                    <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
                    <XAxis dataKey="day" tickFormatter={(d: string) => d.slice(5)} tick={axis} axisLine={false} tickLine={false} />
                    <YAxis allowDecimals={false} tick={axis} axisLine={false} tickLine={false} />
                    <RTooltip contentStyle={tip} />
                    <Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                    {channels.map((c) => <Bar key={c} dataKey={c} stackId="a" fill={CH_COLORS[c] ?? "hsl(var(--muted-foreground))"} name={humanize(c)} />)}
                  </BarChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>
          <div className="grid gap-4 lg:grid-cols-3">
            <Card><CardHeader><div><CardTitle>Most requested products</CardTitle></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]">{o.top_products.length ? o.top_products.map((p) => (
                <div key={p.product_id} className="flex justify-between gap-2"><Link className="truncate hover:underline" to={`/app/catalog/${p.product_id}`}>{p.name}</Link><span className="tabular">{p.requests}</span></div>
              )) : <p className="text-muted-foreground">None yet.</p>}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Most requested categories</CardTitle></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]">{o.top_categories.length ? o.top_categories.map((c) => (
                <div key={c.category} className="flex justify-between gap-2"><span className="truncate">{c.category}</span><span className="tabular">{c.requests}</span></div>
              )) : <p className="text-muted-foreground">None yet.</p>}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Consumers</CardTitle><CardDescription>API keys, feed URLs, anonymous agents</CardDescription></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]" data-testid="analytics-consumers">{o.consumers.length ? o.consumers.map((c) => (
                <div key={`${c.type}${c.id}`} className="flex justify-between gap-2"><span className="truncate">{c.label} <span className="text-[11px] text-muted-foreground">({humanize(c.type)})</span></span><span className="tabular">{c.requests}</span></div>
              )) : <p className="text-muted-foreground">None yet.</p>}</CardContent></Card>
          </div>
          {o.recent_errors.length > 0 && (
            <Card className="mt-4 overflow-hidden"><CardHeader><div><CardTitle>Recent errors</CardTitle></div></CardHeader>
              <div className="overflow-x-auto"><Table><THead><TR><TH>When</TH><TH>Channel</TH><TH>Operation</TH><TH>Status</TH><TH>Message</TH></TR></THead>
                <TBody>{o.recent_errors.map((e, i) => (
                  <TR key={i}><TD className="whitespace-nowrap text-muted-foreground">{formatDate(e.at)}</TD><TD>{humanize(e.channel)}</TD><TD>{humanize(e.kind)}</TD><TD>{e.status}</TD><TD className="max-w-[360px] text-[12px]">{e.message}</TD></TR>
                ))}</TBody></Table></div></Card>
          )}
          <Card className="mt-4"><CardContent className="pt-5 text-[12.5px] text-muted-foreground">
            {a.data!.note} Not measured: {a.data!.not_measured.join(", ")}.
          </CardContent></Card>
        </>
      )}
    </>
  );
}
