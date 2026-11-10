import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, Copy, Download, ExternalLink, Lock, Plus, RefreshCw, RotateCw, Rss, Send, ShieldOff, Webhook } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader } from "@/components/app/common";
import { ConfirmDialog } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/overlays";
import { commerce } from "@/services/commerce";
import { DELIVERY_TONE, EVENT_TYPES, FEED_FORMATS, FEED_TYPES, PLATFORM_STATUS_TONE, PROFILES, distribution } from "@/services/distribution";
import type { Feed, FeedSnapshot, Platform, WebhookDelivery } from "@/types/distribution";
import { bytes, formatDate, humanize, timeAgo } from "@/utils/format";
import { copyText } from "@/pages/app/DistributionPages";

const SNAP_TONE: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = { completed: "success", failed: "danger", running: "info", queued: "neutral" };

function SecretOnce({ value, label, onDone }: { value: string; label: string; onDone: () => void }) {
  return (
    <Card className="mb-4 border-warning/40"><CardContent className="space-y-2 pt-5 text-[13px]">
      <p className="font-medium">{label}</p>
      <div className="flex gap-2"><pre className="flex-1 overflow-x-auto rounded bg-muted p-2 font-mono text-[12px]" data-testid="secret-once">{value}</pre><Button size="sm" variant="outline" onClick={() => copyText(value)}><Copy /> Copy</Button></div>
      <Button size="xs" variant="ghost" onClick={onDone}>I've saved it</Button>
    </CardContent></Card>
  );
}

/* ================================================================ Feeds */
function NewFeedDialog({ open, onOpenChange, onCreated, profiles, types }: {
  open: boolean; onOpenChange: (o: boolean) => void; onCreated: () => void; profiles: Record<string, string[]>; types: Record<string, string[]>;
}) {
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => commerce.categories(), enabled: open });
  const [f, setF] = useState({ name: "", profile: "agentready", format: "json", feed_type: "full", category_id: "", schedule: "manual", max_age_hours: 24 });
  const [err, setErr] = useState<unknown>(null);
  const formats = profiles[f.profile] ?? [];
  const ftypes = (types[f.profile] ?? []).filter((t) => t !== "incremental" || ["json", "jsonl"].includes(f.format));
  useEffect(() => {
    if (!formats.includes(f.format)) setF((x) => ({ ...x, format: formats[0] ?? "json" }));
    if (!ftypes.includes(f.feed_type)) setF((x) => ({ ...x, feed_type: "full" }));
  }, [f.profile, f.format]); // eslint-disable-line react-hooks/exhaustive-deps
  const create = async () => {
    setErr(null);
    try {
      await distribution.createFeed({ ...f, category_id: f.feed_type === "category" ? f.category_id || null : null });
      toast.success("Feed created");
      onOpenChange(false); onCreated();
      setF({ ...f, name: "" });
    } catch (e) { setErr(e); }
  };
  const catItems = (cats.data as { items?: { id: string; path: string }[] } | undefined)?.items ?? [];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>New feed</DialogTitle></DialogHeader>
        <div className="space-y-3 text-[13px]">
          <ErrorNote error={err} />
          <Field label="Name" htmlFor="nf-name"><Input id="nf-name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. Full catalog for partners" /></Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Profile" htmlFor="nf-profile"><NativeSelect id="nf-profile" value={f.profile} onChange={(e) => setF({ ...f, profile: e.target.value })}>
              {Object.keys(profiles).map((p) => <option key={p} value={p}>{PROFILES[p] ?? p}</option>)}</NativeSelect></Field>
            <Field label="Format" htmlFor="nf-format"><NativeSelect id="nf-format" value={f.format} onChange={(e) => setF({ ...f, format: e.target.value })}>
              {formats.map((x) => <option key={x} value={x}>{FEED_FORMATS[x] ?? x}</option>)}</NativeSelect></Field>
            <Field label="Contents" htmlFor="nf-type"><NativeSelect id="nf-type" value={f.feed_type} onChange={(e) => setF({ ...f, feed_type: e.target.value })}>
              {ftypes.map((x) => <option key={x} value={x}>{FEED_TYPES[x] ?? x}</option>)}</NativeSelect></Field>
            {f.feed_type === "category" && (
              <Field label="Category" htmlFor="nf-cat"><NativeSelect id="nf-cat" value={f.category_id} onChange={(e) => setF({ ...f, category_id: e.target.value })}>
                <option value="">Choose…</option>{catItems.map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}</NativeSelect></Field>
            )}
            <Field label="Regenerate" htmlFor="nf-sched"><NativeSelect id="nf-sched" value={f.schedule} onChange={(e) => setF({ ...f, schedule: e.target.value })}>
              <option value="manual">Manually</option><option value="hourly">Every hour</option><option value="daily">Every day</option><option value="weekly">Every week</option></NativeSelect></Field>
            <Field label="Expires after (hours)" htmlFor="nf-age" hint="Older snapshots are regenerated before they are served">
              <Input id="nf-age" type="number" min={1} max={720} value={f.max_age_hours} onChange={(e) => setF({ ...f, max_age_hours: Number(e.target.value) })} /></Field>
          </div>
        </div>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={!f.name.trim()} onClick={create}>Create feed</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SnapshotLine({ s }: { s: FeedSnapshot }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[12px]">
      <Badge tone={SNAP_TONE[s.status]} dot>v{s.version} {humanize(s.status)}</Badge>
      {s.status === "running" && <span className="tabular">{s.processed}/{s.total} products</span>}
      {s.status === "completed" && <span className="text-muted-foreground">{s.record_count} records · {bytes(s.size_bytes)}{s.removed_count ? ` · ${s.removed_count} removals` : ""}{s.excluded_count ? ` · ${s.excluded_count} excluded` : ""} · {timeAgo(s.finished_at)}{s.expired ? " · expired" : ""}</span>}
      {s.status === "failed" && <span className="text-destructive">{s.error}</span>}
    </div>
  );
}

function FeedCard({ f, onChange, onSecret }: { f: Feed; onChange: () => void; onSecret: (url: string) => void }) {
  const [history, setHistory] = useState(false);
  const [tokName, setTokName] = useState("");
  const [tokExp, setTokExp] = useState("");
  const [revoke, setRevoke] = useState<string | null>(null);
  const [archive, setArchive] = useState(false);
  const snaps = useQuery({ queryKey: ["feed-snaps", f.id], queryFn: () => distribution.snapshots(f.id), enabled: history,
    refetchInterval: (q) => (q.state.data?.items.some((s) => ["queued", "running"].includes(s.status)) ? 1500 : false) });
  const live = f.latest && ["queued", "running"].includes(f.latest.status);
  const served = f.served ?? null;
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); toast.success(msg); onChange(); } catch (e) { toast.error((e as Error).message); }
  };
  return (
    <Card className={f.archived_at ? "opacity-60" : ""} data-testid="feed-card">
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-2">
        <div>
          <CardTitle className="flex flex-wrap items-center gap-2">{f.name}<Badge>{PROFILES[f.profile]}</Badge><Badge tone="info">{FEED_FORMATS[f.format]}</Badge><Badge>{FEED_TYPES[f.feed_type]}</Badge>{f.archived_at && <Badge tone="danger">Archived</Badge>}</CardTitle>
          <CardDescription>
            {f.schedule === "manual" ? "Generated manually" : `Regenerated ${f.schedule}`}{f.next_run_at ? ` · next ${formatDate(f.next_run_at)}` : ""} · expires after {f.max_age_hours} h · last generated {timeAgo(f.last_generated_at)}
          </CardDescription>
        </div>
        {!f.archived_at && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!!live} onClick={() => act(() => distribution.generate(f.id), "Generation started")}><RefreshCw className={live ? "animate-spin" : ""} /> {live ? "Generating…" : "Generate now"}</Button>
            {served && <Button size="sm" variant="outline" onClick={() => distribution.downloadSnapshot(served).catch((e) => toast.error((e as Error).message))}><Download /> Download v{served.version}</Button>}
            <Button size="sm" variant="ghost" onClick={() => setArchive(true)}>Archive</Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-3 text-[13px]">
        {f.latest ? <SnapshotLine s={f.latest} /> : <p className="text-muted-foreground">Not generated yet.</p>}
        {f.latest?.validation?.error_count ? (
          <details><summary className="cursor-pointer text-[12px] text-warning">{f.latest.validation.error_count} products excluded by validation</summary>
            <ul className="mt-1 max-h-40 list-disc overflow-auto pl-5 text-[12px] text-muted-foreground">{f.latest.validation.errors?.map((e) => <li key={e}>{e}</li>)}</ul></details>
        ) : null}
        {f.latest?.validation?.warning_count ? (
          <details><summary className="cursor-pointer text-[12px] text-muted-foreground">{f.latest.validation.warning_count} warnings</summary>
            <ul className="mt-1 max-h-40 list-disc overflow-auto pl-5 text-[12px] text-muted-foreground">{f.latest.validation.warnings?.map((e) => <li key={e}>{e}</li>)}</ul></details>
        ) : null}
        <div>
          <p className="mb-1 font-medium">Feed URLs</p>
          {f.tokens.length === 0 && <p className="text-[12px] text-muted-foreground">No public URL yet. Create one to share this feed with a partner.</p>}
          <div className="space-y-1">
            {f.tokens.map((t) => (
              <div key={t.id} className={`flex flex-wrap items-center gap-2 text-[12px] ${t.active ? "" : "opacity-60"}`}>
                <span className="font-medium">{t.name}</span><code className="font-mono text-muted-foreground">{t.url_hint}</code>
                <span className="text-muted-foreground">{t.usage_count} downloads · last {timeAgo(t.last_used_at)}{t.expires_at ? ` · expires ${formatDate(t.expires_at)}` : ""}</span>
                {t.active ? (
                  <>
                    <Button size="xs" variant="outline" onClick={() => act(async () => { const r = await distribution.rotateToken(t.id); onSecret(r.url); }, "URL rotated — the old one stopped working")}><RotateCw /> Rotate</Button>
                    <Button size="xs" variant="ghost" onClick={() => setRevoke(t.id)}><ShieldOff /> Revoke</Button>
                  </>
                ) : <Badge tone="danger">{t.revoked_at ? "Revoked" : "Expired"}</Badge>}
              </div>
            ))}
          </div>
          {!f.archived_at && (
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <Input aria-label="Feed URL name" className="w-48" placeholder="URL name, e.g. Partner X" value={tokName} onChange={(e) => setTokName(e.target.value)} />
              <Input aria-label="Expires" type="datetime-local" className="w-52" value={tokExp} onChange={(e) => setTokExp(e.target.value)} />
              <Button size="sm" variant="outline" disabled={!tokName.trim()} onClick={() => act(async () => {
                const r = await distribution.createToken(f.id, { name: tokName.trim(), ...(tokExp ? { expires_at: new Date(tokExp).toISOString() } : {}) });
                onSecret(r.url); setTokName(""); setTokExp("");
              }, "Feed URL created")}><Lock /> Create feed URL</Button>
            </div>
          )}
        </div>
        <Button size="xs" variant="ghost" onClick={() => setHistory(!history)}>{history ? "Hide" : "Show"} generation history</Button>
        {history && (snaps.isLoading ? <Skeleton className="h-16" /> : (
          <div className="space-y-1 rounded border p-2">{snaps.data?.items.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center justify-between gap-2"><SnapshotLine s={s} />
              {s.status === "completed" && <Button size="xs" variant="ghost" onClick={() => distribution.downloadSnapshot(s)}><Download /></Button>}</div>
          ))}</div>
        ))}
      </CardContent>
      <ConfirmDialog open={!!revoke} onOpenChange={(o) => !o && setRevoke(null)} title="Revoke this feed URL?" destructive confirmLabel="Revoke"
        description="Anyone using the URL loses access immediately." onConfirm={async () => { await distribution.revokeToken(revoke!); toast.success("Feed URL revoked"); onChange(); }} />
      <ConfirmDialog open={archive} onOpenChange={setArchive} title="Archive this feed?" destructive confirmLabel="Archive"
        description="Scheduled generation stops and all of its URLs are revoked. History is kept." onConfirm={async () => { await distribution.archiveFeed(f.id); toast.success("Feed archived"); onChange(); }} />
    </Card>
  );
}

export function FeedsPage() {
  const qc = useQueryClient();
  const feeds = useQuery({ queryKey: ["feeds"], queryFn: distribution.feeds,
    refetchInterval: (q) => (q.state.data?.items.some((f) => f.latest && ["queued", "running"].includes(f.latest.status)) ? 1500 : false) });
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["feeds"] }); void qc.invalidateQueries({ queryKey: ["feed-snaps"] }); };
  return (
    <>
      <PageHeader title="Product feeds" description="Versioned snapshots of your distributed catalog in JSON, JSON Lines, CSV, Schema.org JSON-LD or XML."
        actions={<Button onClick={() => setOpen(true)}><Plus /> New feed</Button>} />
      {secret && <SecretOnce value={secret} label="Copy this feed URL now. It contains a secret token that cannot be shown again." onDone={() => setSecret(null)} />}
      <ErrorNote error={feeds.error} />
      {feeds.isLoading ? <Skeleton className="h-48" /> : !feeds.data?.items.length ? (
        <EmptyState icon={<Rss />} title="No feeds yet" description="Create a feed, generate it, then share a revocable URL with partners or platforms." action={<Button size="sm" onClick={() => setOpen(true)}><Plus /> New feed</Button>} />
      ) : (
        <div className="space-y-4">{feeds.data.items.map((f) => <FeedCard key={f.id} f={f} onChange={refresh} onSecret={setSecret} />)}</div>
      )}
      <p className="mt-4 text-[12px] text-muted-foreground">A feed or JSON-LD file does not by itself make products appear in any AI platform; each platform decides inclusion under its own programme (see AI platforms).</p>
      {feeds.data && <NewFeedDialog open={open} onOpenChange={setOpen} onCreated={refresh} profiles={feeds.data.profiles} types={feeds.data.profile_types} />}
    </>
  );
}

/* ================================================================ AI platforms */
function PlatformCard({ p, feeds, onChange }: { p: Platform; feeds: Feed[]; onChange: () => void }) {
  const [busy, setBusy] = useState("");
  const run = async (key: string, fn: () => Promise<unknown>, msg?: string) => {
    setBusy(key);
    try { await fn(); if (msg) toast.success(msg); onChange(); } catch (e) { toast.error((e as Error).message); onChange(); } finally { setBusy(""); }
  };
  const cap = p.capabilities;
  const v = p.last_validation;
  const compatible = feeds.filter((f) => f.profile === p.feed_profile && !f.archived_at);
  return (
    <Card data-testid={`platform-${p.provider}`}>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-2">
        <div><CardTitle className="flex flex-wrap items-center gap-2">{p.name}<Badge tone={PLATFORM_STATUS_TONE[p.status]} dot>{humanize(p.status)}</Badge>
          {cap.live_status === "blocked" && <Badge tone="warning">Live submission blocked</Badge>}{cap.live_status === "not_available" && <Badge>No public API</Badge>}</CardTitle>
          <CardDescription className="break-words">{cap.delivery}</CardDescription></div>
        {p.provider !== "mcp_clients" && (
          <label className="flex items-center gap-2 text-[13px]"><Checkbox checked={p.enabled} aria-label={`Use ${p.name}`} onCheckedChange={(x) => run("en", () => distribution.updatePlatform(p.provider, { enabled: !!x }))} /> Prepare for this platform</label>
        )}
      </CardHeader>
      <CardContent className="space-y-3 text-[13px]">
        {cap.blocked_reason && <p className="rounded border border-warning/30 bg-warning/5 p-2 text-[12.5px]">{cap.blocked_reason}</p>}
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <p className="mb-1 font-medium">Official requirements</p>
            <ul className="list-disc space-y-0.5 pl-5 text-[12.5px] text-muted-foreground">{cap.requirements.map((r) => <li key={r}>{r}</li>)}</ul>
            <p className="mt-2 text-[12px] text-muted-foreground">Checkout: {cap.checkout}. Regions: {cap.regions}. Formats: {cap.feed_formats.join(", ") || "—"}.</p>
            <div className="mt-1 flex flex-wrap gap-2">{cap.docs.map((d) => <a key={d} href={d} target="_blank" rel="noreferrer" className="inline-flex min-w-0 items-center gap-1 break-all text-[12px] text-info hover:underline"><ExternalLink className="h-3 w-3 shrink-0" />{new URL(d).hostname}{new URL(d).pathname.length > 1 ? new URL(d).pathname : ""}</a>)}</div>
          </div>
          <div>
            {p.onboarding.length > 0 && <>
              <p className="mb-1 font-medium">Setup</p>
              <div className="space-y-1">{p.onboarding.map((s) => (
                <div key={s.step} className="flex items-start gap-2 text-[12.5px]">{s.done ? <CheckCircle2 className="mt-0.5 h-4 w-4 text-success" /> : s.blocked ? <Lock className="mt-0.5 h-4 w-4 text-warning" /> : <Circle className="mt-0.5 h-4 w-4 text-muted-foreground" />}<span>{s.step}</span></div>
              ))}</div>
            </>}
            {p.missing_setup.length > 0 && <p className="mt-2 text-[12px] text-warning">Missing: {p.missing_setup.map(humanize).join(", ")}</p>}
          </div>
        </div>
        {p.feed_profile && (
          <div className="flex flex-wrap items-end gap-2 border-t pt-3">
            <Field label="Feed" htmlFor={`pf-${p.provider}`}>
              <NativeSelect id={`pf-${p.provider}`} value={p.feed_id ?? ""} onChange={(e) => run("feed", () => distribution.updatePlatform(p.provider, { feed_id: e.target.value || null }), "Feed linked")}>
                <option value="">Create automatically when validating</option>{compatible.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
              </NativeSelect>
            </Field>
            <Button size="sm" loading={busy === "val"} onClick={() => run("val", () => distribution.validatePlatform(p.provider), "Feed generated and validated")}>Generate & validate feed</Button>
            {p.provider !== "perplexity" && (
              <Button size="sm" variant="outline" onClick={() => run("app", () => distribution.application(p.provider, !p.application_submitted_at))}>
                {p.application_submitted_at ? "Mark application withdrawn" : "I have applied / added the feed"}
              </Button>
            )}
            <Button size="sm" variant="ghost" loading={busy === "sub"} onClick={() => run("sub", () => distribution.submitPlatform(p.provider))}><Send /> Submit catalog</Button>
          </div>
        )}
        {v?.snapshot_id && (
          <div className="rounded border p-2 text-[12.5px]" data-testid="platform-validation">
            <p>{v.valid ? <Badge tone="success">Valid</Badge> : <Badge tone="warning">Needs attention</Badge>} {v.records ?? 0} valid rows · {v.excluded ?? 0} products excluded · validated {timeAgo(p.last_validated_at)}</p>
            {!!v.errors?.length && <ul className="mt-1 max-h-36 list-disc overflow-auto pl-5 text-muted-foreground">{v.errors.map((e) => <li key={e}>{e}</li>)}</ul>}
          </div>
        )}
        {p.history.length > 0 && (
          <details><summary className="cursor-pointer text-[12px] text-muted-foreground">Sync history ({p.history.length})</summary>
            <div className="mt-1 space-y-0.5 text-[12px]">{p.history.map((h) => <p key={h.id}><Badge tone={h.status === "completed" ? "success" : h.status === "blocked" ? "warning" : "danger"}>{humanize(h.action)} {h.status}</Badge> {timeAgo(h.at)} — {h.message}</p>)}</div>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

export function PlatformsPage() {
  const qc = useQueryClient();
  const plats = useQuery({ queryKey: ["platforms"], queryFn: distribution.platforms });
  const feeds = useQuery({ queryKey: ["feeds"], queryFn: distribution.feeds });
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["platforms"] }); void qc.invalidateQueries({ queryKey: ["feeds"] }); };
  return (
    <>
      <PageHeader title="AI platform integrations" description="Prepare your catalog for AI shopping platforms using their official requirements. Statuses reflect only what AgentReady can verify." />
      {plats.data && <Card className="mb-4"><CardContent className="pt-5 text-[13px] text-muted-foreground">{plats.data.disclaimer}</CardContent></Card>}
      <ErrorNote error={plats.error} />
      {plats.isLoading ? <Skeleton className="h-64" /> : <div className="space-y-4">{plats.data?.items.map((p) => <PlatformCard key={p.provider} p={p} feeds={feeds.data?.items ?? []} onChange={refresh} />)}</div>}
    </>
  );
}

/* ================================================================ Webhooks */
function Deliveries({ subId }: { subId: string }) {
  const [status, setStatus] = useState("");
  const [detail, setDetail] = useState<WebhookDelivery | null>(null);
  const qc = useQueryClient();
  const d = useQuery({ queryKey: ["deliveries", subId, status], queryFn: () => distribution.deliveries(subId, { status, page_size: 25 }), refetchInterval: 5000 });
  return (
    <div className="space-y-2">
      <NativeSelect aria-label="Delivery status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-40">
        <option value="">All deliveries</option><option value="succeeded">Succeeded</option><option value="pending">Pending retry</option><option value="failed">Failed</option>
      </NativeSelect>
      {!d.data?.items.length ? <p className="text-[12px] text-muted-foreground">No deliveries yet. Product changes are delivered within a few seconds.</p> : (
        <div className="overflow-x-auto rounded border">
          <Table>
            <THead><TR><TH>When</TH><TH>Event</TH><TH>Status</TH><TH>Attempts</TH><TH>Response</TH><TH /></TR></THead>
            <TBody>{d.data.items.map((x) => (
              <TR key={x.id} data-testid="delivery-row">
                <TD className="whitespace-nowrap text-muted-foreground">{formatDate(x.created_at)}</TD>
                <TD className="font-mono text-[11.5px]">{x.event_type}{x.replay_number ? <span className="ml-1 text-muted-foreground">(replay {x.replay_number})</span> : null}</TD>
                <TD><Badge tone={DELIVERY_TONE[x.status]} dot>{humanize(x.status)}</Badge>{x.status === "pending" && x.next_attempt_at && <p className="text-[11px] text-muted-foreground">retry {timeAgo(x.next_attempt_at)}</p>}</TD>
                <TD className="tabular">{x.attempts}/{x.max_attempts}</TD>
                <TD className="text-[12px]">{x.last_status_code ?? "—"}{x.last_error ? ` · ${x.last_error}` : ""}{x.last_response_ms != null ? ` · ${x.last_response_ms} ms` : ""}</TD>
                <TD className="text-right">
                  <div className="flex justify-end gap-1">
                    <Button size="xs" variant="ghost" onClick={async () => setDetail(await distribution.delivery(x.id))}>Details</Button>
                    {["succeeded", "failed"].includes(x.status) && x.event_type !== "webhook.test" && (
                      <Button size="xs" variant="outline" onClick={async () => { try { await distribution.replay(x.id); toast.success("Queued for redelivery (same webhook-id)"); void qc.invalidateQueries({ queryKey: ["deliveries", subId] }); } catch (e) { toast.error((e as Error).message); } }}>Replay</Button>
                    )}
                  </div>
                </TD>
              </TR>
            ))}</TBody>
          </Table>
        </div>
      )}
      <Dialog open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Delivery {detail?.event_id.slice(0, 8)}</DialogTitle></DialogHeader>
          {detail && <div className="space-y-2 text-[12px]">
            <p><b>webhook-id:</b> <code className="font-mono">{detail.event_id}</code></p>
            <div><b>Attempts</b>{detail.attempt_log.map((a, i) => <p key={i} className="text-muted-foreground">{formatDate(a.at)} — {a.status ?? "no response"} {a.error ?? ""} ({a.ms} ms)</p>)}</div>
            <pre className="max-h-80 overflow-auto rounded bg-muted p-2 font-mono text-[11px]">{JSON.stringify(detail.payload, null, 2)}</pre>
          </div>}
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function WebhooksPage() {
  const qc = useQueryClient();
  const subs = useQuery({ queryKey: ["webhooks"], queryFn: distribution.webhooks });
  const [url, setUrl] = useState("");
  const [desc, setDesc] = useState("");
  const [events, setEvents] = useState<string[]>(["*"]);
  const [err, setErr] = useState<unknown>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [disable, setDisable] = useState<string | null>(null);
  const refresh = () => void qc.invalidateQueries({ queryKey: ["webhooks"] });
  const create = async () => {
    setErr(null);
    try {
      const r = await distribution.createWebhook({ url: url.trim(), events, description: desc || null });
      setSecret(r.secret); setUrl(""); setDesc(""); setOpen(r.id); refresh();
      toast.success("Webhook created");
    } catch (e) { setErr(e); }
  };
  const signing = subs.data?.signing as { scheme?: string; headers?: string[]; signed_content?: string; tolerance_seconds?: number } | undefined;
  const retry = subs.data?.retry_policy as { max_attempts?: number; backoff?: string } | undefined;
  return (
    <>
      <PageHeader title="Webhooks" description="Notify partners when distributed products change. Payloads contain only the public product representation." />
      {secret && <SecretOnce value={secret} label="Copy this signing secret now. It cannot be shown again (rotate it if it is lost)." onDone={() => setSecret(null)} />}
      <Card className="mb-6">
        <CardHeader><div><CardTitle>New subscription</CardTitle><CardDescription>HTTPS endpoints on public addresses only. Each delivery is signed; retries use exponential backoff.</CardDescription></div></CardHeader>
        <CardContent className="space-y-3 text-[13px]">
          <ErrorNote error={err} />
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="Endpoint URL" htmlFor="wh-url"><Input id="wh-url" placeholder="https://partner.example/hooks/agentready" value={url} onChange={(e) => setUrl(e.target.value)} /></Field>
            <Field label="Description" htmlFor="wh-desc"><Input id="wh-desc" value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
          </div>
          <Field label="Events">
            <div className="flex flex-wrap gap-3">
              <label className="flex items-center gap-1.5"><Checkbox checked={events.includes("*")} onCheckedChange={(v) => setEvents(v ? ["*"] : [])} /> All events</label>
              {EVENT_TYPES.map((e) => (
                <label key={e} className="flex items-center gap-1.5 font-mono text-[12px]"><Checkbox disabled={events.includes("*")} checked={events.includes("*") || events.includes(e)}
                  onCheckedChange={(v) => setEvents(v ? [...events, e] : events.filter((x) => x !== e))} /> {e}</label>
              ))}
            </div>
          </Field>
          <Button disabled={!url.trim() || !events.length} onClick={create}><Webhook /> Create webhook</Button>
          {signing && <p className="text-[12px] text-muted-foreground">Signing: {signing.scheme}; headers {signing.headers?.join(", ")}; signed content <code>{signing.signed_content}</code>; reject timestamps older than {signing.tolerance_seconds}s. Retries: up to {retry?.max_attempts} attempts, {retry?.backoff}.</p>}
        </CardContent>
      </Card>
      <ErrorNote error={subs.error} />
      {subs.isLoading ? <Skeleton className="h-32" /> : !subs.data?.items.length ? (
        <EmptyState icon={<Webhook />} title="No webhooks yet" />
      ) : (
        <div className="space-y-4">{subs.data.items.map((s) => (
          <Card key={s.id} className={s.is_active ? "" : "opacity-60"} data-testid="webhook-card">
            <CardHeader className="flex-row flex-wrap items-start justify-between gap-2">
              <div><CardTitle className="flex flex-wrap items-center gap-2 break-all">{s.url}{s.is_active ? <Badge tone="success" dot>Active</Badge> : <Badge dot>Disabled</Badge>}{s.consecutive_failures > 0 && <Badge tone="danger">{s.consecutive_failures} failures in a row</Badge>}</CardTitle>
                <CardDescription>{s.events.join(", ")} · secret {s.secret_hint}{s.rotation_grace_until ? ` · previous secret valid until ${formatDate(s.rotation_grace_until)}` : ""} · last success {timeAgo(s.last_success_at)}</CardDescription></div>
              <div className="flex flex-wrap gap-2">
                <Button size="xs" variant="outline" onClick={async () => { try { const d = await distribution.testWebhook(s.id); d.status === "succeeded" ? toast.success(`Test delivered (${d.last_status_code})`) : toast.error(`Test failed: ${d.last_error}`); refresh(); } catch (e) { toast.error((e as Error).message); } }}><Send /> Send test</Button>
                <Button size="xs" variant="outline" onClick={async () => { try { const r = await distribution.rotateSecret(s.id); setSecret(r.secret); refresh(); } catch (e) { toast.error((e as Error).message); } }}><RotateCw /> Rotate secret</Button>
                {s.is_active ? <Button size="xs" variant="ghost" onClick={() => setDisable(s.id)}>Disable</Button>
                  : <Button size="xs" variant="ghost" onClick={async () => { await distribution.updateWebhook(s.id, { is_active: true }); refresh(); }}>Enable</Button>}
                <Button size="xs" variant="ghost" onClick={() => setOpen(open === s.id ? null : s.id)}>{open === s.id ? "Hide" : "Deliveries"}</Button>
              </div>
            </CardHeader>
            {open === s.id && <CardContent><Deliveries subId={s.id} /></CardContent>}
          </Card>
        ))}</div>
      )}
      <ConfirmDialog open={!!disable} onOpenChange={(o) => !o && setDisable(null)} title="Disable this webhook?" confirmLabel="Disable"
        description="No further events are sent. Pending retries are dropped." onConfirm={async () => { await distribution.disableWebhook(disable!); toast.success("Webhook disabled"); refresh(); }} />
    </>
  );
}
