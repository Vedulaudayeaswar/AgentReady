import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Copy, KeyRound, Plug, RefreshCw, RotateCw, ScrollText, Search, ShieldOff, Upload } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader, Pagination } from "@/components/app/common";
import { ConfirmDialog, display } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/overlays";
import { commerce } from "@/services/commerce";
import type { Conflict, Integration, SyncSchedule } from "@/types/commerce";
import { formatDate, humanize, timeAgo } from "@/utils/format";

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = {
  completed: "success", completed_with_errors: "warning", awaiting_review: "warning", failed: "danger", running: "info",
  queued: "neutral", cancelled: "neutral", connected: "success", error: "danger", pending: "neutral",
};
const SBadge = ({ s }: { s: string }) => <Badge tone={STATUS_TONE[s] ?? "neutral"} dot>{humanize(s)}</Badge>;
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const TZS = ["UTC", "Asia/Kolkata", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Asia/Singapore", "Australia/Sydney"];

/* ================================================================ Synchronization */
function ScheduleCard({ s, methods, kinds, onSaved }: { s: SyncSchedule; methods: string[]; kinds: string[]; onSaved: () => void }) {
  const [f, setF] = useState<SyncSchedule>(s);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => setF(s), [s]);
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const tzs = Array.from(new Set([f.timezone, browserTz, ...TZS]));
  const set = (k: keyof SyncSchedule, v: unknown) => setF({ ...f, [k]: v } as SyncSchedule);
  return (
    <Card>
      <CardHeader><div><CardTitle className="truncate">{s.website_url}</CardTitle><CardDescription>{s.enabled && s.next_run_at ? `Next scheduled scan ${formatDate(s.next_run_at)}` : "Manual scans only"}{s.last_error ? ` · ${s.last_error}` : ""}</CardDescription></div></CardHeader>
      <CardContent className="space-y-4">
        <ErrorNote error={err} />
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Frequency" htmlFor={`f-${s.website_id}`}>
            <NativeSelect id={`f-${s.website_id}`} className="w-full" value={f.frequency} onChange={(e) => setF({ ...f, frequency: e.target.value as SyncSchedule["frequency"], enabled: e.target.value !== "manual" })}>
              <option value="manual">Manual</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option>
            </NativeSelect>
          </Field>
          {f.frequency === "weekly" && (
            <Field label="Day" htmlFor={`wd-${s.website_id}`}><NativeSelect id={`wd-${s.website_id}`} className="w-full" value={f.weekday} onChange={(e) => set("weekday", Number(e.target.value))}>{WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}</NativeSelect></Field>
          )}
          {f.frequency === "monthly" && (
            <Field label="Day of month" htmlFor={`dm-${s.website_id}`}><NativeSelect id={`dm-${s.website_id}`} className="w-full" value={f.day_of_month} onChange={(e) => set("day_of_month", Number(e.target.value))}>{Array.from({ length: 28 }, (_, i) => <option key={i} value={i + 1}>{i + 1}</option>)}</NativeSelect></Field>
          )}
          {f.frequency !== "manual" && (
            <>
              <Field label="Time" htmlFor={`t-${s.website_id}`}><Input id={`t-${s.website_id}`} type="time" value={`${String(f.hour).padStart(2, "0")}:${String(f.minute).padStart(2, "0")}`}
                onChange={(e) => { const [h, m] = e.target.value.split(":").map(Number); setF({ ...f, hour: h || 0, minute: m || 0 }); }} /></Field>
              <Field label="Timezone" htmlFor={`tz-${s.website_id}`}><NativeSelect id={`tz-${s.website_id}`} className="w-full" value={f.timezone} onChange={(e) => set("timezone", e.target.value)}>{tzs.map((t) => <option key={t}>{t}</option>)}</NativeSelect></Field>
            </>
          )}
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Max pages" htmlFor={`mp-${s.website_id}`}><Input id={`mp-${s.website_id}`} inputMode="numeric" value={f.max_pages ?? ""} onChange={(e) => set("max_pages", e.target.value ? Number(e.target.value) : null)} /></Field>
          <Field label="Max products" htmlFor={`mx-${s.website_id}`} hint="Empty = system limit"><Input id={`mx-${s.website_id}`} inputMode="numeric" value={f.max_products ?? ""} onChange={(e) => set("max_products", e.target.value ? Number(e.target.value) : null)} /></Field>
          <Field label="When a website change conflicts with your edits" htmlFor={`cp-${s.website_id}`}>
            <NativeSelect id={`cp-${s.website_id}`} className="w-full" value={f.conflict_policy} onChange={(e) => set("conflict_policy", e.target.value)}>
              <option value="review">Ask me (recommended)</option><option value="keep_agentready">Always keep my value</option><option value="accept_website">Always take the website value</option>
            </NativeSelect>
          </Field>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="mb-1.5 text-[12.5px] font-medium">Extraction methods</p>
            <div className="flex flex-wrap gap-3">
              {methods.map((m) => (
                <label key={m} className="flex items-center gap-1.5 text-[12.5px]">
                  <Checkbox checked={f.extraction_methods.includes(m)} aria-label={humanize(m)}
                    onCheckedChange={(v) => set("extraction_methods", v ? [...f.extraction_methods, m] : f.extraction_methods.filter((x) => x !== m))} />
                  {m === "ai" ? "AI (Groq)" : m === "html" ? "HTML heuristics" : humanize(m)}
                </label>
              ))}
            </div>
          </div>
          <div>
            <p className="mb-1.5 text-[12.5px] font-medium">Detect changes to</p>
            <div className="flex flex-wrap gap-3">
              {kinds.map((k) => (
                <label key={k} className="flex items-center gap-1.5 text-[12.5px]">
                  <Checkbox checked={f.detect[k] !== false} aria-label={humanize(k)} onCheckedChange={(v) => set("detect", { ...f.detect, [k]: !!v })} />{humanize(k)}
                </label>
              ))}
            </div>
          </div>
        </div>
        <label className="flex items-center gap-2 text-[12.5px]"><Checkbox checked={f.auto_apply} onCheckedChange={(v) => set("auto_apply", !!v)} aria-label="Auto-apply" />
          Apply non-conflicting changes automatically (otherwise every rescan waits for your review)</label>
        <div className="flex flex-wrap justify-end gap-2">
          <Button size="sm" variant="outline" onClick={async () => {
            try { await commerce.startSync({ website_id: s.website_id }); toast.success("Rescan started"); onSaved(); } catch (e) { toast.error((e as Error).message); }
          }}><RefreshCw /> Sync now</Button>
          <Button size="sm" loading={busy} disabled={!f.extraction_methods.length} onClick={async () => {
            setBusy(true);
            setErr(null);
            try {
              // send only editable settings (the API schema rejects unknown fields)
              const keys = ["frequency", "enabled", "timezone", "hour", "minute", "weekday", "day_of_month", "max_pages", "max_products",
                "extraction_methods", "detect", "conflict_policy", "auto_apply"] as const;
              const body = Object.fromEntries(keys.map((k) => [k, f[k]]));
              await commerce.saveSchedule({ ...body, website_id: s.website_id });
              toast.success("Sync settings saved");
              onSaved();
            } catch (e) {
              setErr(e);
            } finally {
              setBusy(false);
            }
          }}>Save settings</Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ConflictRow({ c, onDone }: { c: Conflict; onDone: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const resolve = async (action: string, v?: string) => {
    try {
      await commerce.resolveConflict(c.id, action, v);
      toast.success("Conflict resolved");
      onDone();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };
  const field = c.field === "price_amount" ? "price" : c.field;
  return (
    <div className="rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Link to={`/app/catalog/${c.product_id}`} className="font-medium hover:underline">{c.product_name}</Link>
        <Badge>{humanize(field)}</Badge><Badge tone="info">{humanize(c.source_type)}</Badge>
        <span className="text-[12px] text-muted-foreground">detected {timeAgo(c.detected_at)}</span>
      </div>
      <div className="mt-2 grid gap-2 text-[13px] sm:grid-cols-3">
        <div className="rounded bg-muted/40 p-2"><p className="text-[11.5px] text-muted-foreground">Website now</p><p className="break-words">{display(c.website_value)}</p></div>
        <div className="rounded bg-muted/40 p-2"><p className="text-[11.5px] text-muted-foreground">AgentReady (yours)</p><p className="break-words font-medium">{display(c.agentready_value)}</p></div>
        <div className="rounded bg-muted/40 p-2"><p className="text-[11.5px] text-muted-foreground">Website before</p><p className="break-words">{display(c.original_value)}</p></div>
      </div>
      {c.evidence[0]?.source_url && (
        <p className="mt-1.5 truncate text-[11.5px] text-muted-foreground">Evidence: <a className="underline" href={c.evidence[0].source_url} target="_blank" rel="noopener noreferrer">{c.evidence[0].source_url}</a>{c.evidence[0].source_value ? ` — “${String(c.evidence[0].source_value).slice(0, 120)}”` : ""}</p>
      )}
      <p className="mt-1.5 text-[12px]"><span className="font-medium">Suggested: {c.suggested_action === "keep_agentready" ? "Keep AgentReady value" : c.suggested_action === "accept_website" ? "Accept website value" : humanize(c.suggested_action)}.</span> <span className="text-muted-foreground">{c.suggestion_reason}</span></p>
      {c.status === "open" ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Button size="xs" variant={c.suggested_action === "accept_website" ? "default" : "outline"} onClick={() => resolve("accept_website")}>Accept website value</Button>
          <Button size="xs" variant={c.suggested_action === "keep_agentready" ? "default" : "outline"} onClick={() => resolve("keep_agentready")}>Keep AgentReady value</Button>
          {!["images", "attributes", "dimensions", "weight"].includes(c.field) && <Button size="xs" variant="outline" onClick={() => setEditing(!editing)}>Edit manually</Button>}
          <Button size="xs" variant="ghost" onClick={() => resolve("ignore")}>Ignore</Button>
          {editing && (
            <div className="flex w-full gap-2 pt-1">
              <Input className="h-8" aria-label="New value" value={value} onChange={(e) => setValue(e.target.value)} placeholder={`New ${field}`} />
              <Button size="xs" disabled={!value.trim()} onClick={() => resolve("edit", value.trim())}>Save</Button>
            </div>
          )}
        </div>
      ) : <p className="mt-2 text-[12px] text-muted-foreground">Resolved: {humanize(c.resolution ?? "")}</p>}
    </div>
  );
}

export function SyncPage() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [conflictStatus, setConflictStatus] = useState("open");
  const schedules = useQuery({ queryKey: ["sync-schedules"], queryFn: commerce.schedulesSync });
  const syncs = useQuery({ queryKey: ["syncs", page], queryFn: () => commerce.syncs({ page, page_size: 15 }), placeholderData: keepPreviousData, refetchInterval: 5000 });
  const conflicts = useQuery({ queryKey: ["conflicts", conflictStatus], queryFn: () => commerce.conflicts(conflictStatus) });
  const refresh = () => {
    for (const k of ["sync-schedules", "syncs", "conflicts", "products", "overview"]) void qc.invalidateQueries({ queryKey: [k] });
  };
  return (
    <>
      <PageHeader title="Synchronization" description="Scheduled rescans, synchronization history and conflicts between your website or store and AgentReady." />
      <Card className="mb-6">
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2"><AlertTriangle className="h-4 w-4 text-warning" /> Conflicts</CardTitle>
            <CardDescription>A source changed a value you manage in AgentReady. Nothing is overwritten until you decide.</CardDescription>
          </div>
          <NativeSelect aria-label="Conflict status" value={conflictStatus} onChange={(e) => setConflictStatus(e.target.value)}>
            <option value="open">Open</option><option value="resolved">Resolved</option><option value="all">All</option>
          </NativeSelect>
        </CardHeader>
        <CardContent className="space-y-2">
          {conflicts.isLoading ? <Skeleton className="h-20" /> : !conflicts.data?.items.length ? (
            <p className="text-[13px] text-muted-foreground">{conflictStatus === "open" ? "No open conflicts." : "None."}</p>
          ) : conflicts.data.items.map((c) => <ConflictRow key={c.id} c={c} onDone={refresh} />)}
        </CardContent>
      </Card>

      <h2 className="mb-3 text-sm font-semibold">Website schedules & settings</h2>
      {schedules.isLoading ? <Skeleton className="mb-6 h-40" /> : !schedules.data?.items.length ? (
        <div className="mb-6"><EmptyState icon={<RefreshCw />} title="No websites connected" action={<Button asChild size="sm"><Link to="/app/connect">Connect website</Link></Button>} /></div>
      ) : (
        <div className="mb-6 space-y-4">
          {schedules.data.items.map((s) => <ScheduleCard key={s.website_id} s={s} methods={schedules.data.extraction_methods} kinds={schedules.data.detect_kinds} onSaved={refresh} />)}
        </div>
      )}

      <Card className="overflow-hidden">
        <CardHeader><CardTitle>Synchronization history</CardTitle></CardHeader>
        {!syncs.data?.items.length ? <CardContent><p className="text-[13px] text-muted-foreground">No syncs yet.</p></CardContent> : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead><TR><TH>Started</TH><TH>Source</TH><TH>Direction</TH><TH>Trigger</TH><TH>Status</TH><TH className="text-right">Discovered</TH><TH className="text-right">Created</TH><TH className="text-right">Changed</TH><TH className="text-right">Ignored</TH><TH className="text-right">Conflicts</TH><TH className="text-right">Failed</TH><TH>Finished</TH></TR></THead>
                <TBody>
                  {syncs.data.items.map((j) => (
                    <TR key={j.id}>
                      <TD className="whitespace-nowrap text-muted-foreground">{timeAgo(j.started_at ?? j.created_at)}</TD>
                      <TD className="max-w-[200px] truncate">{j.scan_id ? <Link to={`/app/scans/${j.scan_id}`} className="hover:underline">{j.source_label}</Link> : j.source_label}<p className="text-[11.5px] text-muted-foreground">{humanize(j.source_type)}</p></TD>
                      <TD>{humanize(j.direction)}</TD>
                      <TD>{humanize(j.trigger)}</TD>
                      <TD><SBadge s={j.status} /></TD>
                      {[j.products_discovered, j.products_created, j.products_changed, j.products_ignored, j.conflicts_detected, j.failed_operations].map((n, i) => <TD key={i} className="tabular text-right">{n}</TD>)}
                      <TD className="whitespace-nowrap text-muted-foreground">{j.finished_at ? formatDate(j.finished_at) : "—"}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <div className="border-t"><Pagination page={page} pageSize={15} total={syncs.data.total} onPage={setPage} /></div>
          </>
        )}
      </Card>
    </>
  );
}

/* ================================================================ Integrations */
function ConnectDialog({ provider, open, onOpenChange, onDone, oauth }: { provider: "woocommerce" | "shopify"; open: boolean; onOpenChange: (o: boolean) => void; onDone: () => void; oauth: boolean }) {
  const [f, setF] = useState<Record<string, string>>({});
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState<{ secret: string; setup: string } | null>(null);
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) { setF({}); setErr(null); setSecret(null); } }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Connect {provider === "woocommerce" ? "WooCommerce" : "Shopify"}</DialogTitle>
          <DialogDescription>{provider === "woocommerce"
            ? "Create REST API keys in WooCommerce → Settings → Advanced → REST API (Read/Write). Keys are encrypted and never shown again."
            : oauth ? "Enter your myshopify.com domain to authorise AgentReady, or paste an Admin API access token from a custom app."
              : "Shopify OAuth is not configured on this server (no app credentials). You can connect with a custom-app Admin API access token."}</DialogDescription>
        </DialogHeader>
        <ErrorNote error={err} />
        {secret ? (
          <div className="space-y-2 text-[13px]">
            <p className="font-medium">Connected. Webhook secret (shown once):</p>
            <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-[12px]">{secret.secret}</pre>
            <p className="text-muted-foreground">{secret.setup}</p>
            <DialogFooter><Button onClick={() => onOpenChange(false)}>Done</Button></DialogFooter>
          </div>
        ) : (
          <>
            {provider === "woocommerce" ? (
              <div className="space-y-3">
                <Field label="Store URL" htmlFor="w-url"><Input id="w-url" placeholder="https://mystore.com" value={f.store_url ?? ""} onChange={(e) => setF({ ...f, store_url: e.target.value })} /></Field>
                <Field label="Consumer key" htmlFor="w-ck"><Input id="w-ck" autoComplete="off" placeholder="ck_…" value={f.consumer_key ?? ""} onChange={(e) => setF({ ...f, consumer_key: e.target.value.trim() })} /></Field>
                <Field label="Consumer secret" htmlFor="w-cs"><Input id="w-cs" type="password" autoComplete="off" placeholder="cs_…" value={f.consumer_secret ?? ""} onChange={(e) => setF({ ...f, consumer_secret: e.target.value.trim() })} /></Field>
              </div>
            ) : (
              <div className="space-y-3">
                <Field label="Shop domain" htmlFor="s-shop"><Input id="s-shop" placeholder="my-store.myshopify.com" value={f.shop ?? ""} onChange={(e) => setF({ ...f, shop: e.target.value.trim() })} /></Field>
                <Field label={oauth ? "Admin API access token (optional)" : "Admin API access token"} htmlFor="s-tok"><Input id="s-tok" type="password" autoComplete="off" placeholder="shpat_…" value={f.access_token ?? ""} onChange={(e) => setF({ ...f, access_token: e.target.value.trim() })} /></Field>
              </div>
            )}
            <Field label="Stock is managed in" htmlFor="c-master">
              <NativeSelect id="c-master" className="w-full" value={f.inventory_master ?? "store"} onChange={(e) => setF({ ...f, inventory_master: e.target.value })}>
                <option value="store">The store (imports update AgentReady stock)</option>
                <option value="agentready">AgentReady (push stock to the store)</option>
              </NativeSelect>
            </Field>
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button loading={busy} onClick={async () => {
                setBusy(true);
                setErr(null);
                try {
                  if (provider === "woocommerce") {
                    const r = await commerce.connectWoo({ ...f, inventory_master: f.inventory_master ?? "store" });
                    if (!r.verification.ok) toast.warning(`Saved, but verification failed: ${r.verification.message}`);
                    else toast.success("WooCommerce connected");
                    onDone();
                    if (r.webhook_secret) setSecret({ secret: r.webhook_secret, setup: r.webhook_setup ?? "" });
                    else onOpenChange(false);
                  } else {
                    const r = await commerce.connectShopify({ shop: f.shop, access_token: f.access_token || undefined, inventory_master: f.inventory_master ?? "store" });
                    if (r.authorize_url) {
                      window.location.href = r.authorize_url;
                      return;
                    }
                    if (r.verification && !r.verification.ok) toast.warning(`Saved, but verification failed: ${r.verification.message}`);
                    else toast.success("Shopify connected");
                    onDone();
                    onOpenChange(false);
                  }
                } catch (e) {
                  setErr(e);
                } finally {
                  setBusy(false);
                }
              }}>Connect</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function IntegrationsPage() {
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const data = useQuery({ queryKey: ["integrations"], queryFn: commerce.integrations });
  const [connect, setConnect] = useState<"woocommerce" | "shopify" | null>(null);
  const [disc, setDisc] = useState<Integration | null>(null);
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["integrations"] }); void qc.invalidateQueries({ queryKey: ["syncs"] }); };
  useEffect(() => { if (params.get("connected")) toast.success(`${humanize(params.get("connected")!)} connected`); }, [params]);
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); toast.success(msg); refresh(); } catch (e) { toast.error((e as Error).message); }
  };
  const providers = data.data?.providers;
  return (
    <>
      <PageHeader title="Integrations" description="Connect existing commerce systems through their official APIs. Credentials are encrypted at rest and never shown again." />
      <ErrorNote error={data.error} />
      <div className="mb-6 grid gap-4 md:grid-cols-2">
        {(["woocommerce", "shopify"] as const).map((p) => (
          <Card key={p}>
            <CardHeader><div><CardTitle>{p === "woocommerce" ? "WooCommerce" : "Shopify"}</CardTitle><CardDescription>{providers?.[p]?.auth}</CardDescription></div></CardHeader>
            <CardContent className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={() => setConnect(p)}><Plug /> Connect {p === "woocommerce" ? "store" : "shop"}</Button>
              {p === "shopify" && providers && !providers.shopify.oauth_configured && <Badge tone="warning">OAuth not configured (needs SHOPIFY_API_KEY/SECRET)</Badge>}
              <a className="text-[12px] text-muted-foreground underline" href={providers?.[p]?.docs} target="_blank" rel="noopener noreferrer">API docs</a>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card className="overflow-hidden">
        <CardHeader><CardTitle>Connected stores</CardTitle></CardHeader>
        {data.isLoading ? <div className="p-4"><Skeleton className="h-20" /></div> : !data.data?.items.length ? (
          <CardContent><p className="text-[13px] text-muted-foreground">No stores connected.</p></CardContent>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <THead><TR><TH>Store</TH><TH>Status</TH><TH>Credentials</TH><TH>Currency</TH><TH>Last sync</TH><TH className="w-80" /></TR></THead>
              <TBody>
                {data.data.items.map((i) => (
                  <TR key={i.id}>
                    <TD><p className="font-medium">{i.name ?? i.store_url}</p><p className="text-[11.5px] text-muted-foreground">{humanize(i.provider)} · {i.store_url}</p></TD>
                    <TD><SBadge s={i.status} />{i.last_error && <p className="mt-1 max-w-[220px] text-[11.5px] text-destructive">{i.last_error}</p>}</TD>
                    <TD className="font-mono text-[12px]">{i.credentials_hint ?? "—"}</TD>
                    <TD>{i.currency ?? "—"}</TD>
                    <TD className="text-muted-foreground">{timeAgo(i.last_sync_at)}</TD>
                    <TD>
                      <div className="flex flex-wrap justify-end gap-1">
                        <Button size="xs" variant="outline" onClick={() => act(() => commerce.verifyIntegration(i.id), "Connection checked")}>Verify</Button>
                        <Button size="xs" variant="outline" disabled={i.status !== "connected"} onClick={() => act(() => commerce.syncIntegration(i.id, { direction: "import" }), "Import started")}><RefreshCw /> Import</Button>
                        <Button size="xs" variant="outline" disabled={i.status !== "connected"} onClick={() => act(() => commerce.syncIntegration(i.id, { direction: "export" }), "AgentReady prices & stock pushed")}><Upload /> Push</Button>
                        <Button size="xs" variant="ghost" onClick={() => setDisc(i)}>Disconnect</Button>
                      </div>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </Card>
      <p className="mt-3 text-[12px] text-muted-foreground">Sync results, including failures and conflicts, appear in <Link to="/app/sync" className="underline">Synchronization</Link>.</p>
      {connect && <ConnectDialog provider={connect} open={!!connect} onOpenChange={(o) => !o && setConnect(null)} onDone={refresh} oauth={!!providers?.shopify.oauth_configured} />}
      <ConfirmDialog open={!!disc} onOpenChange={(o) => !o && setDisc(null)} title={`Disconnect ${disc?.store_url}?`} destructive confirmLabel="Disconnect"
        description="Stored credentials are erased. Products already imported stay in your catalog."
        onConfirm={async () => { await commerce.disconnect(disc!.id); toast.success("Disconnected"); refresh(); }} />
    </>
  );
}

/* ================================================================ API & Feeds */
export function ApiFeedsPage() {
  const qc = useQueryClient();
  const keys = useQuery({ queryKey: ["api-keys"], queryFn: commerce.apiKeys });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["catalog:read", "pricing:read", "inventory:read"]);
  const [shown, setShown] = useState<string | null>(null);
  const [revoke, setRevoke] = useState<string | null>(null);
  const base = `${window.location.origin}/api/v2/catalog`;
  const refresh = () => void qc.invalidateQueries({ queryKey: ["api-keys"] });
  const copy = (t: string) => navigator.clipboard?.writeText(t).then(() => toast.success("Copied"));
  return (
    <>
      <PageHeader title="API & feeds" description="Give agents, marketplaces and partners read-only access to your approved, published products." />
      {shown && (
        <Card className="mb-4 border-warning/40">
          <CardContent className="space-y-2 pt-5 text-[13px]">
            <p className="font-medium">Copy your new API key now — it is stored hashed and cannot be shown again.</p>
            <div className="flex gap-2"><pre className="flex-1 overflow-x-auto rounded bg-muted p-2 font-mono text-[12px]">{shown}</pre><Button size="sm" variant="outline" onClick={() => copy(shown)}><Copy /> Copy</Button></div>
            <Button size="xs" variant="ghost" onClick={() => setShown(null)}>I've saved it</Button>
          </CardContent>
        </Card>
      )}
      <Card className="mb-6 overflow-hidden">
        <CardHeader><div><CardTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4" /> API keys</CardTitle><CardDescription>Cost prices, reserved stock, locations and internal notes are never exposed, whatever the scopes.</CardDescription></div></CardHeader>
        <CardContent className="space-y-3 border-b pb-4">
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Key name" htmlFor="k-name"><Input id="k-name" placeholder="e.g. Shopping agent" value={name} onChange={(e) => setName(e.target.value)} /></Field>
            {Object.entries(keys.data?.scopes ?? {}).map(([s, d]) => (
              <label key={s} className="flex items-center gap-1.5 pb-2 text-[12.5px]" title={d}>
                <Checkbox checked={scopes.includes(s)} aria-label={s} onCheckedChange={(v) => setScopes(v ? [...scopes, s] : scopes.filter((x) => x !== s))} /> {s}
              </label>
            ))}
            <Button size="sm" disabled={!name.trim() || !scopes.includes("catalog:read")} onClick={async () => {
              try { const k = await commerce.createKey({ name: name.trim(), scopes }); setShown(k.key); setName(""); refresh(); } catch (e) { toast.error((e as Error).message); }
            }}>Create key</Button>
          </div>
          {!scopes.includes("catalog:read") && <p className="text-[12px] text-destructive">catalog:read is required.</p>}
        </CardContent>
        {!keys.data?.items.length ? <CardContent><p className="pt-3 text-[13px] text-muted-foreground">No keys yet.</p></CardContent> : (
          <div className="overflow-x-auto">
            <Table>
              <THead><TR><TH>Name</TH><TH>Key</TH><TH>Scopes</TH><TH className="text-right">Requests</TH><TH>Last used</TH><TH>Status</TH><TH /></TR></THead>
              <TBody>
                {keys.data.items.map((k) => (
                  <TR key={k.id} className={k.active ? "" : "opacity-60"}>
                    <TD className="font-medium">{k.name}</TD>
                    <TD className="font-mono text-[12px]">{k.prefix}…</TD>
                    <TD><div className="flex flex-wrap gap-1">{k.scopes.map((s) => <Badge key={s}>{s}</Badge>)}</div></TD>
                    <TD className="tabular text-right">{k.usage_count}<p className="text-[11px] text-muted-foreground">{k.daily_usage?.[0] ? `${k.daily_usage[0].requests} on ${k.daily_usage[0].day}` : ""}</p></TD>
                    <TD className="text-muted-foreground">{timeAgo(k.last_used_at)}</TD>
                    <TD>{k.active ? <Badge tone="success">Active</Badge> : <Badge tone="danger">Revoked</Badge>}</TD>
                    <TD>
                      {k.active && (
                        <div className="flex justify-end gap-1">
                          <Button size="xs" variant="outline" onClick={async () => { try { const r = await commerce.rotateKey(k.id); setShown(r.key); refresh(); toast.success("Key rotated; the old key no longer works"); } catch (e) { toast.error((e as Error).message); } }}><RotateCw /> Rotate</Button>
                          <Button size="xs" variant="ghost" onClick={() => setRevoke(k.id)}><ShieldOff /> Revoke</Button>
                        </div>
                      )}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </Card>
      <Card>
        <CardHeader><div><CardTitle>Endpoints (schema agentready-catalog/2.0)</CardTitle><CardDescription>Send the key as the <code>X-Catalog-Key</code> header. The v1 endpoint <code>/api/v1/catalog</code> keeps working unchanged.</CardDescription></div></CardHeader>
        <CardContent className="space-y-2 text-[12.5px]">
          {[
            ["GET /products", "List published products (q, category_id, brand, product_type, availability, min_price, max_price, updated_since, sort, page)"],
            ["GET /products/{id}", "One product (+ evidence with the evidence:read scope)"],
            ["GET /search?q=", "Search published products"],
            ["GET /categories", "Category tree with product counts"],
            ["GET /products/{id}/variants", "Variants of a product"],
            ["GET /availability", "Compact stock & availability per product/variant"],
            ["GET /pricing", "Selling prices (pricing:read)"],
            ["GET /inventory", "Available quantities (inventory:read)"],
            ["GET /feed?type=full|incremental|category|availability", "Feeds. Incremental: pass updated_since; the response lists removed products and next_updated_since."],
          ].map(([e, d]) => (
            <div key={e} className="grid gap-1 sm:grid-cols-[320px_1fr]"><code className="font-mono">{e}</code><span className="text-muted-foreground">{d}</span></div>
          ))}
          <div className="pt-2">
            <p className="mb-1 font-medium">Example</p>
            <div className="flex gap-2"><pre className="flex-1 overflow-x-auto rounded bg-muted p-2 font-mono text-[11.5px]">{`curl -H "X-Catalog-Key: <your key>" "${base}/feed?type=incremental&updated_since=2026-01-01T00:00:00Z"`}</pre>
              <Button size="sm" variant="outline" onClick={() => copy(`curl -H "X-Catalog-Key: <your key>" "${base}/products"`)}><Copy /></Button></div>
          </div>
        </CardContent>
      </Card>
      <ConfirmDialog open={!!revoke} onOpenChange={(o) => !o && setRevoke(null)} title="Revoke this API key?" destructive confirmLabel="Revoke"
        description="Any client using it stops working immediately. This cannot be undone."
        onConfirm={async () => { await commerce.revokeKey(revoke!); toast.success("Key revoked"); refresh(); }} />
    </>
  );
}

/* ================================================================ Audit logs */
export function AuditPage() {
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [since, setSince] = useState("");
  const [page, setPage] = useState(1);
  const data = useQuery({
    queryKey: ["audit", q, action, actor, since, page],
    queryFn: () => commerce.audit({ q, action, actor_type: actor, since: since ? new Date(since).toISOString() : undefined, page, page_size: 50 }),
    placeholderData: keepPreviousData,
  });
  const groups = Array.from(new Set((data.data?.actions ?? []).map((a) => a.split(".")[0])));
  return (
    <>
      <PageHeader title="Audit log" description="Who changed what and when. Entries are append-only and never contain passwords, API keys or store credentials." />
      <div className="mb-3 flex flex-wrap gap-2">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input className="pl-8" aria-label="Search audit log" placeholder="Search summaries" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        </div>
        <NativeSelect aria-label="Action" value={action} onChange={(e) => { setAction(e.target.value); setPage(1); }}>
          <option value="">All actions</option>
          {groups.map((g) => <option key={g} value={`${g}.`}>{humanize(g)}</option>)}
        </NativeSelect>
        <NativeSelect aria-label="Actor" value={actor} onChange={(e) => { setActor(e.target.value); setPage(1); }}>
          <option value="">Any actor</option><option value="user">People</option><option value="system">System</option><option value="integration">Integrations</option>
        </NativeSelect>
        <Input type="date" aria-label="Since" className="w-40" value={since} onChange={(e) => { setSince(e.target.value); setPage(1); }} />
      </div>
      <ErrorNote error={data.error} />
      <Card className="overflow-hidden">
        {data.isLoading ? <div className="p-4"><Skeleton className="h-40" /></div> : !data.data?.items.length ? (
          <div className="p-6"><EmptyState icon={<ScrollText />} title="No matching entries" /></div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead><TR><TH>When</TH><TH>Actor</TH><TH>Action</TH><TH>Summary</TH><TH>Details</TH></TR></THead>
                <TBody>
                  {data.data.items.map((a) => (
                    <TR key={a.id} className="align-top">
                      <TD className="whitespace-nowrap text-muted-foreground" title={a.created_at}>{formatDate(a.created_at)}</TD>
                      <TD className="whitespace-nowrap">{a.actor ?? "—"}<p className="text-[11px] text-muted-foreground">{humanize(a.actor_type)}</p></TD>
                      <TD className="font-mono text-[11.5px]">{a.action}</TD>
                      <TD className="max-w-[380px]">
                        {a.entity_type === "product" && a.entity_id ? <Link to={`/app/catalog/${a.entity_id}`} className="hover:underline">{a.summary}</Link> : a.summary}
                      </TD>
                      <TD>
                        {Object.keys(a.changes).length > 0 && (
                          <details><summary className="cursor-pointer text-[11.5px] text-muted-foreground">Changes</summary>
                            <pre className="mt-1 max-h-40 max-w-[320px] overflow-auto whitespace-pre-wrap rounded bg-muted/50 p-2 text-[11px]">{JSON.stringify(a.changes, null, 2)}</pre>
                          </details>
                        )}
                        {a.request_id && <p className="font-mono text-[10.5px] text-muted-foreground">req {a.request_id}</p>}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <div className="border-t"><Pagination page={page} pageSize={50} total={data.data.total} onPage={setPage} /></div>
          </>
        )}
      </Card>
    </>
  );
}
