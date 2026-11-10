import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, BarChart, Legend, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { Bell, Bot, Download, FileText, Send, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader, Stat } from "@/components/app/common";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, NativeSelect, Skeleton, Switch, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { REPORT_KINDS, intelligence } from "@/services/intelligence";
import type { AssistantAnswer, Suggestion } from "@/services/intelligence";
import { commerce } from "@/services/commerce";
import { bytes, formatDate, humanize, timeAgo } from "@/utils/format";

const axis = { fontSize: 11, fill: "hsl(var(--muted-foreground))" };
const tip = { background: "hsl(var(--popover))", border: "1px solid hsl(var(--border))", borderRadius: 6, fontSize: 12 };
type Row = Record<string, any>;
const Plink = ({ p }: { p: Row | null }) => p ? <Link className="hover:underline" to={`/app/catalog/${p.id}`}>{p.name}</Link> : <>—</>;
const Simple = ({ title, rows, empty, cols }: { title: string; rows: Row[]; empty: string; cols: [string, (r: Row) => React.ReactNode][] }) => (
  <Card className="overflow-hidden"><CardHeader><div><CardTitle>{title}</CardTitle></div></CardHeader>
    {!rows.length ? <CardContent><p className="text-[13px] text-muted-foreground">{empty}</p></CardContent> : (
      <div className="overflow-x-auto"><Table><THead><TR>{cols.map(([h]) => <TH key={h}>{h}</TH>)}</TR></THead>
        <TBody>{rows.map((r, i) => <TR key={i}>{cols.map(([h, f]) => <TD key={h} className="text-[12.5px]">{f(r)}</TD>)}</TR>)}</TBody></Table></div>)}</Card>
);

/* ================================================================ Pricing */
export function PricingInsightsPage() {
  const [days, setDays] = useState(30);
  const d = useQuery({ queryKey: ["intel-pricing", days], queryFn: () => intelligence.pricing(days) });
  const x = d.data;
  return (
    <>
      <PageHeader title="Pricing insights" description={x?.note} actions={<NativeSelect aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))}><option value={7}>7 days</option><option value={30}>30 days</option><option value={90}>90 days</option></NativeSelect>} />
      <ErrorNote error={d.error} />
      {!x ? <Skeleton className="h-64" /> : (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <Stat label="Price changes" value={x.recent_changes.length} /><Stat label="Scheduled changes" value={x.scheduled_changes.length} />
            <Stat label="Sales ending in 7 days" value={x.sales_ending_soon.length} /><Stat label="Invalid prices" value={x.invalid_prices.length} />
            <Stat label="Website price conflicts" value={x.website_price_conflicts.length} />
          </div>
          <Card><CardHeader><div><CardTitle>Price-change trend</CardTitle><CardDescription>Entries in the immutable price history, by source</CardDescription></div></CardHeader>
            <CardContent className="h-[200px]">{!x.daily_changes.length ? <p className="text-[13px] text-muted-foreground">No price changes in this period.</p> : (
              <ResponsiveContainer width="100%" height="100%"><BarChart data={x.daily_changes} margin={{ left: -16, right: 8 }}>
                <XAxis dataKey="day" tick={axis} tickFormatter={(v: string) => v.slice(5)} /><YAxis allowDecimals={false} tick={axis} /><RTooltip contentStyle={tip} /><Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                {Array.from(new Set(x.daily_changes.flatMap((r: Row) => Object.keys(r).filter((k) => k !== "day")))).map((k, i) => (
                  <Bar key={k as string} dataKey={k as string} stackId="a" name={humanize(k as string)} fill={["hsl(var(--foreground))", "hsl(var(--info))", "hsl(var(--success))", "hsl(var(--warning))", "hsl(var(--destructive))"][i % 5]} />))}
              </BarChart></ResponsiveContainer>)}</CardContent></Card>
          <Simple title="Recent price changes" rows={x.recent_changes} empty="No price changes." cols={[["Product", (r) => <Plink p={r.product} />], ["Type", (r) => r.price_type], ["Change", (r) => `${r.old ?? "—"} → ${r.new ?? "—"} ${r.currency ?? ""}`], ["Source", (r) => humanize(r.change_type)], ["When", (r) => formatDate(r.at)]]} />
          <Simple title="Scheduled price changes" rows={x.scheduled_changes} empty="Nothing scheduled." cols={[["Product", (r) => <Plink p={r.product} />], ["New price", (r) => `${r.new ?? "—"} ${r.currency ?? ""}`], ["Effective", (r) => formatDate(r.effective_at)]]} />
          <Simple title="Sales ending soon" rows={x.sales_ending_soon} empty="No sales ending in the next 7 days." cols={[["Product", (r) => <Plink p={r} />], ["Sale price", (r) => r.sale_price], ["Ends", (r) => formatDate(r.ends_at)]]} />
          <Simple title="Invalid or conflicting prices" rows={[...x.invalid_prices.map((r: Row) => ({ ...r, why: !r.currency ? "no currency" : "sale price not below regular" })), ...x.website_price_conflicts.map((r: Row) => ({ ...r.product, why: `website shows ${JSON.stringify(r.website)}, you manage ${JSON.stringify(r.managed)}` }))]}
            empty="No invalid or conflicting prices." cols={[["Product", (r) => <Plink p={r} />], ["Issue", (r) => r.why]]} />
        </div>
      )}
    </>
  );
}

/* ================================================================ Inventory */
export function InventoryInsightsPage() {
  const d = useQuery({ queryKey: ["intel-inventory"], queryFn: () => intelligence.inventory(90) });
  const x = d.data;
  return (
    <>
      <PageHeader title="Inventory insights" description="Based on your inventory transaction log. Manual adjustments are not sales." />
      <ErrorNote error={d.error} />
      {!x ? <Skeleton className="h-64" /> : (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <Stat label="Tracked products" value={x.tracked} /><Stat label="Tracking disabled" value={x.untracked} />
            <Stat label="Low stock" value={x.low_stock.length} /><Stat label="Out of stock / backorder" value={x.out_of_stock.length} /><Stat label="Stale stock counts" value={x.stale_stock.length} />
          </div>
          <Card className="border-warning/40"><CardHeader><div><CardTitle>Forecasting</CardTitle></div></CardHeader>
            <CardContent className="text-[13px]" data-testid="forecast"><Badge tone="warning">Insufficient data</Badge> <span className="ml-1">{x.forecast.reason}</span>
              <p className="mt-1 text-muted-foreground">{x.forecast.order_transactions} order transactions · {x.forecast.adjustment_transactions} manual adjustments in {x.period_days} days.</p></CardContent></Card>
          <Card><CardHeader><div><CardTitle>Historical inventory changes</CardTitle><CardDescription>Transactions per day by type</CardDescription></div></CardHeader>
            <CardContent className="h-[200px]">{!x.daily_transactions.length ? <p className="text-[13px] text-muted-foreground">No inventory transactions in this period.</p> : (
              <ResponsiveContainer width="100%" height="100%"><BarChart data={x.daily_transactions} margin={{ left: -16, right: 8 }}>
                <XAxis dataKey="day" tick={axis} tickFormatter={(v: string) => v.slice(5)} /><YAxis allowDecimals={false} tick={axis} /><RTooltip contentStyle={tip} /><Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                {Array.from(new Set(x.daily_transactions.flatMap((r: Row) => Object.keys(r).filter((k) => k !== "day")))).map((k, i) => (
                  <Bar key={k as string} dataKey={k as string} stackId="a" name={humanize(k as string)} fill={["hsl(var(--info))", "hsl(var(--warning))", "hsl(var(--success))", "hsl(var(--destructive))", "hsl(var(--foreground))"][i % 5]} />))}
              </BarChart></ResponsiveContainer>)}</CardContent></Card>
          <Simple title="Low and out of stock" rows={[...x.low_stock, ...x.out_of_stock]} empty="No low or out-of-stock products." cols={[["Product", (r) => <Plink p={r} />], ["SKU", (r) => r.sku ?? "—"], ["Status", (r) => humanize(r.status)]]} />
          <Simple title="Repeated adjustments (possible counting problems)" rows={x.repeated_adjustments} empty="No products with 3+ adjustments." cols={[["Product", (r) => <Plink p={r} />], ["Adjustments", (r) => r.adjustments]]} />
          <Simple title="Stale stock information" rows={x.stale_stock} empty="All tracked stock was updated recently." cols={[["Product", (r) => <Plink p={r} />], ["Last change", (r) => r.last_change ? formatDate(r.last_change) : "never"]]} />
          <Simple title="Inventory tracking disabled" rows={x.tracking_disabled} empty="Every product tracks inventory." cols={[["Product", (r) => <Plink p={r} />], ["SKU", (r) => r.sku ?? "—"]]} />
        </div>
      )}
    </>
  );
}

/* ================================================================ Reports */
export function ReportsPage() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["intel-reports"], queryFn: intelligence.reports });
  const [kind, setKind] = useState<string>("catalog_quality");
  const [format, setFormat] = useState("pdf");
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const kinds = list.data?.kinds ?? Object.fromEntries(REPORT_KINDS.map((k) => [k, humanize(k)]));
  return (
    <>
      <PageHeader title="Reports" description="Downloadable reports with definitions, data coverage and missing-data warnings. Unavailable metrics are never shown as zero." />
      <Card className="mb-4"><CardContent className="flex flex-wrap items-end gap-3 pt-5">
        <Field label="Report" htmlFor="rp-kind"><NativeSelect id="rp-kind" value={kind} onChange={(e) => setKind(e.target.value)}>{Object.entries(kinds).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</NativeSelect></Field>
        <Field label="Format" htmlFor="rp-format"><NativeSelect id="rp-format" value={format} onChange={(e) => setFormat(e.target.value)}><option value="pdf">PDF</option><option value="csv">CSV</option><option value="json">JSON</option></NativeSelect></Field>
        <Field label="Period" htmlFor="rp-days"><NativeSelect id="rp-days" value={days} onChange={(e) => setDays(Number(e.target.value))}><option value={7}>7 days</option><option value={30}>30 days</option><option value={90}>90 days</option><option value={365}>1 year</option></NativeSelect></Field>
        <Button loading={busy} onClick={async () => { setBusy(true); try { await intelligence.createReport({ kind, format, days }); toast.success("Report generated"); void qc.invalidateQueries({ queryKey: ["intel-reports"] }); } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); } }}><FileText /> Generate report</Button>
      </CardContent></Card>
      <ErrorNote error={list.error} />
      <Card className="overflow-hidden">{!list.data?.items.length ? <div className="p-6"><EmptyState icon={<FileText />} title="No reports yet" /></div> : (
        <div className="overflow-x-auto"><Table><THead><TR><TH>Report</TH><TH>Format</TH><TH>Period</TH><TH>Coverage</TH><TH>Generated</TH><TH /></TR></THead>
          <TBody>{list.data.items.map((r) => (
            <TR key={r.id} data-testid="report-row"><TD className="font-medium">{r.title}</TD><TD><Badge>{r.format.toUpperCase()}</Badge></TD>
              <TD className="whitespace-nowrap text-[12px]">{r.period_start?.slice(0, 10)} – {r.period_end?.slice(0, 10)}</TD>
              <TD className="max-w-[300px] text-[12px] text-warning">{(r.coverage.warnings ?? []).join(" ") || <span className="text-muted-foreground">Complete</span>}</TD>
              <TD className="whitespace-nowrap text-muted-foreground">{timeAgo(r.created_at)}</TD>
              <TD className="text-right"><Button size="xs" variant="outline" onClick={() => intelligence.downloadReport(r).catch((e) => toast.error((e as Error).message))}><Download /> {bytes(r.size_bytes)}</Button></TD></TR>))}</TBody></Table></div>)}</Card>
    </>
  );
}

/* ================================================================ Notifications */
export function NotificationsPage() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("active");
  const list = useQuery({ queryKey: ["intel-notifs", status], queryFn: () => intelligence.notifications(status) });
  const settings = useQuery({ queryKey: ["intel-settings"], queryFn: intelligence.settings });
  const [alerts, setAlerts] = useState<Record<string, { enabled: boolean; threshold: number; label: string; unit: string }>>({});
  const [form, setForm] = useState({ stale_after_days: 90, stale_price_days: 180, stale_stock_days: 30, notification_frequency: "immediate" });
  useEffect(() => {
    if (settings.data) {
      setAlerts(settings.data.alerts);
      setForm({ stale_after_days: settings.data.stale_after_days, stale_price_days: settings.data.stale_price_days,
        stale_stock_days: settings.data.stale_stock_days, notification_frequency: settings.data.notification_frequency });
    }
  }, [settings.data]);
  const refresh = () => { for (const k of ["intel-notifs", "intel-unread"]) void qc.invalidateQueries({ queryKey: [k] }); };
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); refresh(); } catch (e) { toast.error((e as Error).message); } };
  return (
    <>
      <PageHeader title="Notifications" description={list.data?.email.note}
        actions={<Button variant="outline" onClick={() => act(intelligence.readAll)}>Mark all read</Button>} />
      <div className="mb-3"><NativeSelect aria-label="Show" value={status} onChange={(e) => setStatus(e.target.value)}><option value="active">Active</option><option value="unread">Unread</option><option value="dismissed">Dismissed (history)</option><option value="all">All</option></NativeSelect></div>
      <ErrorNote error={list.error} />
      {list.isLoading ? <Skeleton className="h-40" /> : !list.data?.items.length ? <EmptyState icon={<Bell />} title="No notifications" description="Alerts appear here when the intelligence job finds something that needs attention." /> : (
        <div className="space-y-2">{list.data.items.map((n) => (
          <Card key={n.id} className={n.status === "unread" ? "border-info/40" : ""} data-testid="notification">
            <CardContent className="flex flex-wrap items-start justify-between gap-2 pt-4 text-[13px]">
              <div className="min-w-0"><p className="font-medium"><Badge tone={n.severity === "critical" ? "danger" : n.severity === "warning" ? "warning" : "info"}>{n.label}</Badge> {n.title}{n.count > 1 ? <span className="text-muted-foreground"> ×{n.count}</span> : null}</p>
                <p className="text-muted-foreground">{n.body}</p><p className="text-[11px] text-muted-foreground">{timeAgo(n.last_occurred_at)} · email {humanize(n.email_status)}</p></div>
              <div className="flex gap-1">{n.link && <Button asChild size="xs" variant="outline"><Link to={n.link}>Open</Link></Button>}
                {n.status === "unread" && <Button size="xs" variant="ghost" onClick={() => act(() => intelligence.readNotification(n.id))}>Mark read</Button>}
                {n.status !== "dismissed" && <Button size="xs" variant="ghost" onClick={() => act(() => intelligence.dismissNotification(n.id))}>Dismiss</Button>}</div>
            </CardContent></Card>))}</div>
      )}
      <Card className="mt-6"><CardHeader><div><CardTitle>Alert settings</CardTitle><CardDescription>Repeated issues are grouped into one notification. Thresholds avoid noise.</CardDescription></div></CardHeader>
        <CardContent className="space-y-3 text-[13px]">
          <div className="grid gap-2 md:grid-cols-2">{Object.entries(alerts).map(([k, a]) => (
            <div key={k} className="flex items-center justify-between gap-2 rounded border px-3 py-2">
              <label className="flex items-center gap-2"><Switch aria-label={a.label} checked={a.enabled} onCheckedChange={(v) => setAlerts({ ...alerts, [k]: { ...a, enabled: v } })} /> {a.label}</label>
              <span className="flex items-center gap-1 text-[12px] text-muted-foreground">≥ <Input aria-label={`${a.label} threshold`} type="number" className="h-7 w-16" value={a.threshold} onChange={(e) => setAlerts({ ...alerts, [k]: { ...a, threshold: Number(e.target.value) } })} /> {a.unit}</span>
            </div>))}</div>
          <div className="flex flex-wrap gap-3">
            <Field label="Product stale after (days)" htmlFor="ns-a"><Input id="ns-a" type="number" value={form.stale_after_days} onChange={(e) => setForm({ ...form, stale_after_days: Number(e.target.value) })} /></Field>
            <Field label="Price stale after (days)" htmlFor="ns-p"><Input id="ns-p" type="number" value={form.stale_price_days} onChange={(e) => setForm({ ...form, stale_price_days: Number(e.target.value) })} /></Field>
            <Field label="Stock stale after (days)" htmlFor="ns-s"><Input id="ns-s" type="number" value={form.stale_stock_days} onChange={(e) => setForm({ ...form, stale_stock_days: Number(e.target.value) })} /></Field>
            <Field label="Frequency" htmlFor="ns-f"><NativeSelect id="ns-f" value={form.notification_frequency} onChange={(e) => setForm({ ...form, notification_frequency: e.target.value })}><option value="immediate">As they happen (checked every 10 minutes)</option><option value="daily">At most daily</option></NativeSelect></Field>
          </div>
          <Button onClick={() => act(async () => { await intelligence.updateSettings({ ...form, alerts: Object.fromEntries(Object.entries(alerts).map(([k, a]) => [k, { enabled: a.enabled, threshold: a.threshold }])) }); toast.success("Settings saved"); void qc.invalidateQueries({ queryKey: ["intel-settings"] }); })}>Save settings</Button>
        </CardContent></Card>
    </>
  );
}

/* ================================================================ AI assistant */
const EXAMPLES = ["Why is my catalog quality score low?", "Which products have incomplete specifications?", "Which products are missing descriptions?",
  "What should I improve before distributing my catalog?", "Which products have outdated information?", "Why are certain products not appearing in agent search results?",
  "Which products receive the most external search requests?"];

function SuggestionCard({ s, onDone }: { s: Suggestion; onDone: () => void }) {
  const [text, setText] = useState(s.proposed.description ?? "");
  const [keys, setKeys] = useState<string[]>((s.proposed.attributes ?? []).map((a) => a.key));
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, msg: string) => { setBusy(true); try { await fn(); toast.success(msg); onDone(); } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); } };
  return (
    <Card data-testid="suggestion"><CardHeader><div><CardTitle className="flex flex-wrap items-center gap-2"><Sparkles className="h-4 w-4" />Proposed {s.kind} for <Link className="hover:underline" to={`/app/catalog/${s.product_id}`}>{s.product_name}</Link>
      <Badge tone={s.source === "groq" ? "warning" : "neutral"}>{s.label}</Badge></CardTitle>
      <CardDescription>{s.grounding.note ?? s.rationale}{s.grounding.rejected_model_output ? ` — rejected AI output: ${(s.grounding.issues ?? []).join("; ")}` : ""}</CardDescription></div></CardHeader>
      <CardContent className="space-y-3 text-[13px]">
        {s.kind === "description" ? (
          <div className="grid gap-3 lg:grid-cols-2" data-testid="before-after">
            <div><p className="mb-1 font-medium">Before</p><pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 text-[12px]">{s.original.description || "(empty)"}</pre></div>
            <div><p className="mb-1 font-medium">After (editable)</p><Textarea aria-label="Proposed description" rows={10} value={text} onChange={(e) => setText(e.target.value)} /></div>
          </div>
        ) : (
          <div className="space-y-1">{!(s.proposed.attributes ?? []).length && <p className="text-muted-foreground">No attribute values were found in the product's own text.</p>}
            {(s.proposed.attributes ?? []).map((a) => (
              <label key={a.key} className="flex items-start gap-2"><Checkbox checked={keys.includes(a.key)} onCheckedChange={(v) => setKeys(v ? [...keys, a.key] : keys.filter((k) => k !== a.key))} />
                <span><b>{a.label}</b>: {a.value}{a.unit ? ` ${a.unit}` : ""} <span className="text-[11.5px] text-muted-foreground">from “{a.source_snippet}”</span></span></label>))}</div>
        )}
        {s.missing_information.length > 0 && <div><p className="font-medium">Please supply (not invented)</p><ul className="list-disc pl-5 text-muted-foreground">{s.missing_information.map((m) => <li key={m}>{m}</li>)}</ul></div>}
        {s.status === "pending" ? (
          <div className="flex gap-2">
            <Button size="sm" loading={busy} onClick={() => run(() => intelligence.approve(s.id, s.kind === "description" ? { description: text } : { attribute_keys: keys }), "Approved and applied to the product")}>Approve & apply</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => run(() => intelligence.reject(s.id), "Suggestion rejected")}>Reject</Button>
          </div>) : <Badge>{humanize(s.status)}</Badge>}
      </CardContent></Card>
  );
}

export function AssistantPage() {
  const qc = useQueryClient();
  const [question, setQuestion] = useState("");
  const [conv, setConv] = useState<string | null>(null);
  const [answers, setAnswers] = useState<(AssistantAnswer & { question: string })[]>([]);
  const [busy, setBusy] = useState(false);
  const [productId, setProductId] = useState("");
  const products = useQuery({ queryKey: ["assistant-products"], queryFn: () => commerce.products({ page_size: 100 }) });
  const sugs = useQuery({ queryKey: ["intel-sugs"], queryFn: () => intelligence.suggestions("pending") });
  const ask = async (q: string) => {
    if (!q.trim()) return;
    setBusy(true);
    try { const a = await intelligence.ask({ question: q, conversation_id: conv }); setConv(a.conversation_id); setAnswers((x) => [...x, { ...a, question: q }]); setQuestion(""); }
    catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  };
  const suggest = async (kind: "description" | "attributes") => {
    if (!productId) return;
    setBusy(true);
    try { await intelligence.suggest({ product_id: productId, kind }); void qc.invalidateQueries({ queryKey: ["intel-sugs"] }); toast.success("Suggestion ready for review"); }
    catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <>
      <PageHeader title="AI catalog assistant" description="Answers come from your own catalog data. AI wording is checked against that data; anything it cannot ground is replaced by a data-only answer." />
      <div className="grid min-w-0 gap-4 xl:grid-cols-[1.3fr_1fr]">
        <div className="space-y-3">
          {answers.length === 0 && <div className="flex flex-wrap gap-2">{EXAMPLES.map((e) => <Button key={e} size="xs" variant="outline" className="h-auto whitespace-normal py-1 text-left" onClick={() => ask(e)}>{e}</Button>)}</div>}
          {answers.map((a) => (
            <Card key={a.message_id} data-testid="assistant-answer">
              <CardContent className="space-y-2 pt-4 text-[13px]">
                <p className="font-medium">You: {a.question}</p>
                <div className="flex flex-wrap items-center gap-2"><Badge tone={a.source === "groq" ? "warning" : "neutral"}>{a.label}</Badge><Badge>Intent: {humanize(a.intent)}</Badge>
                  {a.grounding.rejected_model_output && <Badge tone="danger">AI output rejected by grounding check</Badge>}</div>
                <p className="whitespace-pre-line">{a.answer}</p>
                {a.grounding.note && <p className="text-[12px] text-muted-foreground">{a.grounding.note}</p>}
                {a.data_gaps.length > 0 && <p className="text-[12px] text-warning">Not available in your data: {a.data_gaps.join("; ")}</p>}
                <details open><summary className="cursor-pointer text-[12px] text-muted-foreground">Evidence: referenced products ({a.facts.products?.length ?? 0})</summary>
                  <div className="mt-1 space-y-0.5" data-testid="assistant-evidence">{(a.facts.products ?? []).map((p) => (
                    <p key={p.id} className="text-[12px]"><Link className="text-info hover:underline" to={`/app/catalog/${p.id}`}>{p.name}</Link> · score {p.score} · {p.missing_required.join(", ") || "no missing required fields"}</p>))}</div></details>
                <details><summary className="cursor-pointer text-[12px] text-muted-foreground">Retrieved data</summary><pre className="max-h-56 overflow-auto rounded bg-muted p-2 text-[11px]">{JSON.stringify({ ...a.facts, products: undefined }, null, 2)}</pre></details>
                <div className="flex flex-wrap gap-2">{a.next_actions.map((n, i) => n.link ? <Button key={i} asChild size="xs" variant="outline"><Link to={n.link}>{n.label}</Link></Button> : <Badge key={i}>{n.label}</Badge>)}</div>
              </CardContent></Card>))}
          <div className="flex gap-2"><Input aria-label="Ask the assistant" placeholder="Ask about your catalog…" value={question} onChange={(e) => setQuestion(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask(question)} />
            <Button loading={busy} onClick={() => ask(question)}><Send /> Ask</Button></div>
        </div>
        <div className="space-y-3">
          <Card><CardHeader><div><CardTitle className="flex items-center gap-2"><Bot className="h-4 w-4" /> Content assistance</CardTitle><CardDescription>Drafts never add specifications, prices, warranties or claims that are not already in your data. Nothing changes until you approve.</CardDescription></div></CardHeader>
            <CardContent className="flex flex-wrap items-end gap-2 text-[13px]">
              <Field label="Product" htmlFor="as-p" className="min-w-0 max-w-full"><NativeSelect id="as-p" className="max-w-full" value={productId} onChange={(e) => setProductId(e.target.value)}><option value="">Choose a product…</option>{(products.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</NativeSelect></Field>
              <Button size="sm" disabled={!productId || busy} onClick={() => suggest("description")}>Suggest description</Button>
              <Button size="sm" variant="outline" disabled={!productId || busy} onClick={() => suggest("attributes")}>Suggest attributes</Button>
            </CardContent></Card>
          {(sugs.data?.items ?? []).map((s) => <SuggestionCard key={s.id} s={s} onDone={() => { void qc.invalidateQueries({ queryKey: ["intel-sugs"] }); }} />)}
          {sugs.data && !sugs.data.items.length && <p className="text-[12px] text-muted-foreground">No proposed edits waiting for review.</p>}
        </div>
      </div>
    </>
  );
}
