import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, Brain, Lightbulb, RefreshCw, Search, SearchX } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader, Pagination, Stat } from "@/components/app/common";
import { ConfirmDialog } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/overlays";
import { LEVEL_TONE, PRIORITY_TONE, SEVERITY_TONE, intelligence } from "@/services/intelligence";
import type { Anomaly, Level, Recommendation } from "@/services/intelligence";
import { formatDate, humanize, timeAgo } from "@/utils/format";

const axis = { fontSize: 11, fill: "hsl(var(--muted-foreground))" };
const tip = { background: "hsl(var(--popover))", border: "1px solid hsl(var(--border))", borderRadius: 6, fontSize: 12 };
const LEVEL_COLORS: Record<Level, string> = { excellent: "hsl(var(--success))", good: "hsl(var(--info))", needs_improvement: "hsl(var(--warning))", critical: "hsl(var(--destructive))" };

export function LevelBadge({ level, score }: { level: Level; score?: number }) {
  return <Badge tone={LEVEL_TONE[level]} dot>{humanize(level)}{score !== undefined ? ` · ${score}` : ""}</Badge>;
}
function Kind({ k }: { k: "catalog" | "commerce" | "observed" | "inferred" }) {
  const label = { catalog: "Catalog metric", commerce: "Commerce management", observed: "Observed agent activity", inferred: "Inferred recommendation" }[k];
  return <Badge tone={k === "observed" ? "info" : k === "inferred" ? "warning" : "neutral"}>{label}</Badge>;
}
const Days = ({ value, onChange }: { value: number; onChange: (n: number) => void }) => (
  <NativeSelect aria-label="Reporting period" value={value} onChange={(e) => onChange(Number(e.target.value))}>
    <option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
  </NativeSelect>
);

/* ================================================================ Overview */
export function IntelligenceOverviewPage() {
  const [days, setDays] = useState(30);
  const qc = useQueryClient();
  const ov = useQuery({ queryKey: ["intel-overview", days], queryFn: () => intelligence.overview(days), placeholderData: keepPreviousData });
  const [busy, setBusy] = useState(false);
  const recompute = async () => {
    setBusy(true);
    try { await intelligence.refresh(); toast.success("Intelligence refreshed"); void qc.invalidateQueries({ queryKey: ["intel-overview"] }); }
    catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  };
  if (ov.isLoading) return <><PageHeader title="Commerce Intelligence" /><Skeleton className="h-72" /></>;
  if (!ov.data) return <><PageHeader title="Commerce Intelligence" /><ErrorNote error={ov.error} /></>;
  const { catalog: c, commerce: m, observed: o, inferred: inf } = ov.data;
  const levels = Object.entries(c.levels as Record<Level, number>).map(([k, v]) => ({ name: humanize(k), key: k as Level, value: v }));
  const dims = Object.entries(c.dimensions as Record<string, { label: string; average: number | null }>).filter(([, v]) => v.average != null)
    .map(([k, v]) => ({ name: humanize(k), average: v.average }));
  return (
    <>
      <PageHeader title="Commerce Intelligence" description="What to improve next, from your real catalog, commerce and agent-activity data."
        actions={<><Days value={days} onChange={setDays} /><Button variant="outline" loading={busy} onClick={recompute}><RefreshCw /> Refresh analysis</Button></>} />
      {c.products === 0 ? (
        <EmptyState icon={<Brain />} title="No products to analyse yet" description="Import a website or add products; intelligence appears as soon as there is data." />
      ) : (
        <>
          <div className="mb-2 flex items-center gap-2"><Kind k="catalog" /></div>
          <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="intel-catalog-stats">
            <Stat label="Products managed" value={c.products} />
            <Stat label="Catalog quality" value={c.average_score ?? "—"} hint="average score, 0-100" />
            <Stat label="Need attention" value={c.attention} hint="critical or needs improvement" />
            <Stat label="Stale information" value={c.stale} />
            <Stat label="Missing specifications" value={c.missing_specifications} />
            <Stat label="Distribution-ready" value={c.distribution_ready} />
          </div>
          <div className="mb-2 flex flex-wrap items-center gap-2"><Kind k="commerce" /></div>
          <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-5">
            <Stat label="Price changes (7 days)" value={m.price_changes_7d} />
            <Stat label="Low stock" value={m.low_stock} />
            <Stat label="Out of stock" value={m.out_of_stock} />
            <Stat label="Sync conflicts" value={m.open_conflicts} />
            <Stat label="Open anomalies" value={m.open_anomalies} />
          </div>
          <div className="mb-2 flex flex-wrap items-center gap-2"><Kind k="observed" /><span className="text-[12px] text-muted-foreground">{o.disclaimer}</span></div>
          <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-3">
            <Stat label={`Agent searches (${days} days)`} value={o.searches} />
            <Stat label="Product detail requests" value={o.product_requests} />
            <Stat label="Zero-result searches" value={o.zero_result_queries} />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card><CardHeader><div><CardTitle>Catalog quality trend</CardTitle><CardDescription>Daily snapshots of the average score</CardDescription></div></CardHeader>
              <CardContent className="h-[220px]">{c.trend.length < 2 ? <p className="text-[13px] text-muted-foreground">Trend needs at least two daily snapshots ({c.trend.length} so far).</p> : (
                <ResponsiveContainer width="100%" height="100%"><AreaChart data={c.trend} margin={{ left: -16, right: 8, top: 4 }}>
                  <CartesianGrid vertical={false} stroke="hsl(var(--border))" /><XAxis dataKey="day" tick={axis} tickFormatter={(d: string) => d.slice(5)} />
                  <YAxis domain={[0, 100]} tick={axis} /><RTooltip contentStyle={tip} />
                  <Area dataKey="average_score" name="Average score" stroke="hsl(var(--foreground))" fill="hsl(var(--foreground) / 0.08)" />
                </AreaChart></ResponsiveContainer>)}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Product status distribution</CardTitle><CardDescription>Quality levels</CardDescription></div></CardHeader>
              <CardContent className="h-[220px]"><ResponsiveContainer width="100%" height="100%"><PieChart>
                <Pie data={levels} dataKey="value" nameKey="name" innerRadius={50} outerRadius={80}>{levels.map((l) => <Cell key={l.key} fill={LEVEL_COLORS[l.key]} />)}</Pie>
                <Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} /><RTooltip contentStyle={tip} /></PieChart></ResponsiveContainer></CardContent></Card>
            <Card><CardHeader><div><CardTitle>Completeness by category</CardTitle><CardDescription>Average quality per category</CardDescription></div></CardHeader>
              <CardContent className="h-[220px]"><ResponsiveContainer width="100%" height="100%"><BarChart data={c.categories} layout="vertical" margin={{ left: 20, right: 8 }}>
                <XAxis type="number" domain={[0, 100]} tick={axis} /><YAxis type="category" dataKey="category" tick={axis} width={110} />
                <RTooltip contentStyle={tip} /><Bar dataKey="average" name="Average score" fill="hsl(var(--info))" /></BarChart></ResponsiveContainer></CardContent></Card>
            <Card><CardHeader><div><CardTitle>Attribute & dimension completeness</CardTitle><CardDescription>Average per quality dimension</CardDescription></div></CardHeader>
              <CardContent className="h-[220px]"><ResponsiveContainer width="100%" height="100%"><BarChart data={dims} margin={{ left: -16, right: 8 }}>
                <XAxis dataKey="name" tick={axis} interval={0} angle={-30} textAnchor="end" height={60} /><YAxis domain={[0, 100]} tick={axis} />
                <RTooltip contentStyle={tip} /><Bar dataKey="average" name="Average" fill="hsl(var(--success))" /></BarChart></ResponsiveContainer></CardContent></Card>
            <Card><CardHeader><div><CardTitle>Information freshness</CardTitle><CardDescription>Products by time since last update</CardDescription></div></CardHeader>
              <CardContent className="h-[220px]"><ResponsiveContainer width="100%" height="100%"><BarChart data={c.freshness} margin={{ left: -16, right: 8 }}>
                <XAxis dataKey="bucket" tick={axis} /><YAxis allowDecimals={false} tick={axis} /><RTooltip contentStyle={tip} />
                <Bar dataKey="products" name="Products" fill="hsl(var(--warning))" /></BarChart></ResponsiveContainer></CardContent></Card>
            <Card><CardHeader><div><CardTitle>Distribution activity trend</CardTitle><CardDescription>Observed agent requests per day</CardDescription></div></CardHeader>
              <CardContent className="h-[220px]">{!o.trend.length ? <p className="text-[13px] text-muted-foreground">No agent activity recorded in this period.</p> : (
                <ResponsiveContainer width="100%" height="100%"><BarChart data={o.trend} margin={{ left: -16, right: 8 }}>
                  <XAxis dataKey="day" tick={axis} tickFormatter={(d: string) => d.slice(5)} /><YAxis allowDecimals={false} tick={axis} />
                  <RTooltip contentStyle={tip} /><Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="searches" stackId="a" name="Searches" fill="hsl(var(--foreground))" />
                  <Bar dataKey="product_requests" stackId="a" name="Product requests" fill="hsl(var(--info))" />
                  <Bar dataKey="mcp_calls" stackId="a" name="MCP calls" fill="hsl(var(--success))" />
                  <Bar dataKey="feed_downloads" stackId="a" name="Feed downloads" fill="hsl(var(--warning))" />
                </BarChart></ResponsiveContainer>)}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Demand signals: most requested products</CardTitle><CardDescription>Agent requests, not purchases</CardDescription></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]">{o.top_products.length ? o.top_products.map((p: Record<string, any>) => (
                <div key={p.product_id} className="flex justify-between gap-2"><Link className="truncate hover:underline" to={`/app/catalog/${p.product_id}`}>{p.name}</Link><span className="tabular">{p.total}</span></div>
              )) : <p className="text-muted-foreground">No product requests recorded.</p>}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Category-level search interest</CardTitle><CardDescription>Searches filtered by category and product requests per category</CardDescription></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]">{[...o.category_search_interest.map((x: any) => ({ c: x.category, n: x.count, t: "searches" })), ...o.top_categories.map((x: any) => ({ c: x.category, n: x.requests, t: "requests" }))].length ? (
                [...o.category_search_interest.map((x: any) => ({ c: x.category, n: x.count, t: "searches" })), ...o.top_categories.map((x: any) => ({ c: x.category, n: x.requests, t: "product requests" }))].map((x, i) => (
                  <div key={i} className="flex justify-between gap-2"><span className="truncate">{x.c}</span><span className="tabular text-muted-foreground">{x.n} {x.t}</span></div>))
              ) : <p className="text-muted-foreground">No category activity recorded.</p>}</CardContent></Card>
          </div>
          <Card className="mt-4"><CardHeader><div><CardTitle className="flex items-center gap-2"><Lightbulb className="h-4 w-4" /> Top recommendations <Kind k="inferred" /></CardTitle>
            <CardDescription>{inf.note}</CardDescription></div></CardHeader>
            <CardContent className="space-y-2 text-[13px]">{inf.top.length ? inf.top.map((r: Recommendation) => (
              <div key={r.id} className="flex flex-wrap items-center gap-2"><Badge tone={PRIORITY_TONE[r.priority]}>{humanize(r.priority)}</Badge><span>{r.title}</span><span className="text-muted-foreground">({r.product_ids.length} products)</span></div>
            )) : <p className="text-muted-foreground">No open recommendations.</p>}
              <Button asChild size="sm" variant="outline"><Link to="/app/intelligence/recommendations">All recommendations</Link></Button></CardContent></Card>
          <p className="mt-4 text-[12px] text-muted-foreground">Not measured: {ov.data.not_measured.join(", ")}. AgentReady does not see what happens after an agent receives your data.</p>
        </>
      )}
    </>
  );
}

/* ================================================================ Quality drill-down */
export function ProductQualityDialog({ productId, onClose }: { productId: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["intel-pq", productId], queryFn: () => intelligence.productQuality(productId!), enabled: !!productId });
  const d = q.data;
  return (
    <Dialog open={!!productId} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader><DialogTitle>Quality report{d ? `: ${d.name}` : ""}</DialogTitle></DialogHeader>
        {q.isLoading ? <Skeleton className="h-48" /> : !d ? <ErrorNote error={q.error} /> : (
          <div className="space-y-4 text-[13px]" data-testid="product-quality">
            <div className="flex flex-wrap items-center gap-3"><span className="text-2xl font-semibold tabular">{d.score}</span><LevelBadge level={d.level} />
              <Link className="text-info hover:underline" to={`/app/catalog/${d.product_id}`}>Open product</Link></div>
            <div className="grid gap-3 md:grid-cols-2">
              <div><p className="font-medium">Missing required</p>{d.missing_required.length ? <ul className="list-disc pl-5" data-testid="missing-required">{d.missing_required.map((f) => <li key={f}>{f.replace("attribute:", "attribute: ")}</li>)}</ul> : <p className="text-muted-foreground">None</p>}</div>
              <div><p className="font-medium">Missing optional</p>{d.missing_optional.length ? <ul className="list-disc pl-5">{d.missing_optional.map((f) => <li key={f}>{f}</li>)}</ul> : <p className="text-muted-foreground">None</p>}</div>
              <div><p className="font-medium">Invalid</p>{d.invalid.length ? <ul className="list-disc pl-5">{d.invalid.map((f) => <li key={f}>{f}</li>)}</ul> : <p className="text-muted-foreground">None</p>}</div>
              <div><p className="font-medium">Stale</p>{d.stale.length ? <ul className="list-disc pl-5">{d.stale.map((f) => <li key={f}>{f}</li>)}</ul> : <p className="text-muted-foreground">None</p>}</div>
              <div><p className="font-medium">Distribution blockers</p>{d.blockers.length ? <ul className="list-disc pl-5">{d.blockers.map((f) => <li key={f}>{f}</li>)}</ul> : <p className="text-muted-foreground">None</p>}</div>
              <div><p className="font-medium">Withheld by you (not penalised)</p>{d.withheld.length ? <ul className="list-disc pl-5">{d.withheld.map((f) => <li key={f}>{f}</li>)}</ul> : <p className="text-muted-foreground">None</p>}</div>
            </div>
            <div><p className="font-medium">Required attributes for this category</p><p className="text-muted-foreground">{d.required_attributes.length ? d.required_attributes.join(", ") : "None defined (define them in Categories → Attributes)"}</p></div>
            <div className="overflow-x-auto rounded border"><Table>
              <THead><TR><TH>Dimension</TH><TH className="text-right">Score</TH><TH className="text-right">Weight</TH><TH>Why</TH></TR></THead>
              <TBody>{Object.entries(d.dimensions).map(([k, v]) => (
                <TR key={k}><TD className="font-medium">{humanize(k)}</TD><TD className="tabular text-right">{v.applies ? v.score : "n/a"}</TD><TD className="tabular text-right">{v.weight}</TD>
                  <TD className="text-[12px] text-muted-foreground">{v.issues.join("; ") || (v.applies ? "OK" : "Does not apply")}</TD></TR>
              ))}</TBody></Table></div>
            {d.suggestions.length > 0 && <div><p className="font-medium">Suggested improvements</p><ul className="list-disc pl-5">{d.suggestions.map((s) => <li key={s}>{s}</li>)}</ul></div>}
            {d.agent_interest_30d && <p className="text-muted-foreground">Agent requests in 30 days: {d.agent_interest_30d.total} (detail {d.agent_interest_30d.product_detail}, pricing {d.agent_interest_30d.pricing}, availability {d.agent_interest_30d.availability}, compare {d.agent_interest_30d.compare})</p>}
            <p className="text-[12px] text-muted-foreground">The score measures completeness, validity, freshness and provenance — not whether the values are factually correct.</p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/* ================================================================ Catalog quality */
export function QualityPage() {
  const [params] = useSearchParams();
  const [level, setLevel] = useState(params.get("level") ?? "");
  const [filter, setFilter] = useState(params.get("filter") ?? "");
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<string | null>(null);
  const data = useQuery({ queryKey: ["intel-quality", level, filter, category, q, page], placeholderData: keepPreviousData,
    queryFn: () => intelligence.quality({ level, filter, category, q, page, page_size: 25, sort: "score" }) });
  const cats = useQuery({ queryKey: ["intel-cats"], queryFn: intelligence.categories });
  const d = data.data;
  return (
    <>
      <PageHeader title="Catalog quality" description={d?.methodology.formula} />
      <ErrorNote error={data.error} />
      {!d ? <Skeleton className="h-64" /> : (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
            <Stat label="Average score" value={d.summary.average_score ?? "—"} />
            {(["excellent", "good", "needs_improvement", "critical"] as Level[]).map((l) => <Stat key={l} label={humanize(l)} value={d.summary.levels[l]} />)}
          </div>
          <div className="mb-4 grid gap-4 lg:grid-cols-2">
            <Card><CardHeader><div><CardTitle>History</CardTitle><CardDescription>Week and month comparisons use real snapshots only</CardDescription></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]">
                {(["week", "month"] as const).map((k) => { const c = d.comparisons[k]; return (
                  <p key={k}><b>This {k} vs last {k}:</b> {c.available ? `score ${c.change!.average_score! >= 0 ? "+" : ""}${c.change!.average_score}, distribution-ready ${c.change!.distribution_ready! >= 0 ? "+" : ""}${c.change!.distribution_ready}, needing attention ${c.change!.attention_products! >= 0 ? "+" : ""}${c.change!.attention_products}` : c.reason}</p>
                ); })}
                <p className="text-muted-foreground">{d.methodology.note}</p>
              </CardContent></Card>
            <Card><CardHeader><div><CardTitle>Category quality</CardTitle><CardDescription>{cats.data?.note}</CardDescription></div></CardHeader>
              <CardContent className="space-y-1 text-[13px]" data-testid="category-quality">{(cats.data?.items ?? []).map((c) => (
                <button key={c.category} className="flex w-full justify-between gap-2 rounded px-1 text-left hover:bg-accent" onClick={() => { setCategory(c.category); setPage(1); }}>
                  <span className="truncate">{c.category} <span className="text-muted-foreground">({c.products})</span></span>
                  <span className="text-right text-[12px]"><b className="tabular">{c.average}</b>{c.missing?.[0] ? <span className="text-muted-foreground"> · most missing: {c.missing[0].field} ({c.missing[0].products})</span> : null}</span>
                </button>))}</CardContent></Card>
          </div>
          <div className="mb-3 flex flex-wrap gap-2">
            <div className="relative w-full sm:w-56"><Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input className="pl-8" aria-label="Search products" placeholder="Search" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} /></div>
            <NativeSelect aria-label="Level" value={level} onChange={(e) => { setLevel(e.target.value); setPage(1); }}>
              <option value="">All levels</option>{(["excellent", "good", "needs_improvement", "critical"] as Level[]).map((l) => <option key={l} value={l}>{humanize(l)}</option>)}</NativeSelect>
            <NativeSelect aria-label="Issue" value={filter} onChange={(e) => { setFilter(e.target.value); setPage(1); }}>
              <option value="">Any issue</option><option value="missing_specifications">Missing specifications</option><option value="stale">Stale</option><option value="blockers">Distribution blockers</option><option value="invalid">Invalid data</option></NativeSelect>
            {category && <Button size="sm" variant="ghost" onClick={() => setCategory("")}>Category: {category} ✕</Button>}
          </div>
          <Card className="overflow-hidden">{!d.items.length ? <div className="p-6"><EmptyState icon={<Search />} title="No matching products" /></div> : (
            <>
              <div className="overflow-x-auto"><Table>
                <THead><TR><TH>Product</TH><TH>Score</TH><TH>Missing required</TH><TH>Top suggestion</TH><TH /></TR></THead>
                <TBody>{d.items.map((r) => (
                  <TR key={r.product_id} data-testid="quality-row">
                    <TD className="max-w-[240px]"><span className="font-medium">{r.name}</span><p className="text-[11.5px] text-muted-foreground">{r.category ?? "Uncategorised"}</p></TD>
                    <TD><LevelBadge level={r.level} score={r.score} /></TD>
                    <TD className="max-w-[240px] text-[12px]">{r.missing_required.join(", ") || "—"}</TD>
                    <TD className="max-w-[280px] text-[12px] text-muted-foreground">{r.suggestions[0] ?? "—"}</TD>
                    <TD className="text-right"><Button size="xs" variant="outline" onClick={() => setOpen(r.product_id)}>Quality report</Button></TD>
                  </TR>))}</TBody></Table></div>
              <div className="border-t"><Pagination page={page} pageSize={25} total={d.total} onPage={setPage} /></div>
            </>)}</Card>
          <Card className="mt-4"><CardHeader><div><CardTitle>Weights</CardTitle><CardDescription>Configurable in Notifications → Settings. Dimensions that do not apply are excluded.</CardDescription></div></CardHeader>
            <CardContent className="flex flex-wrap gap-2 text-[12px]">{Object.entries(d.methodology.weights).map(([k, w]) => <Badge key={k}>{humanize(k)}: {w}</Badge>)}</CardContent></Card>
        </>
      )}
      <ProductQualityDialog productId={open} onClose={() => setOpen(null)} />
    </>
  );
}

/* ================================================================ Discovery */
export function DiscoveryPage() {
  const [days, setDays] = useState(30);
  const ins = useQuery({ queryKey: ["intel-search", days], queryFn: () => intelligence.searchInsights(days) });
  const top = useQuery({ queryKey: ["intel-top", days], queryFn: () => intelligence.topProducts(days) });
  const zero = useQuery({ queryKey: ["intel-zero", days], queryFn: () => intelligence.zeroResults(days) });
  const s = ins.data;
  const list = (rows: { label: string; n: number }[], empty: string) => rows.length ? rows.map((r, i) => (
    <div key={i} className="flex justify-between gap-2"><span className="truncate">{r.label}</span><span className="tabular">{r.n}</span></div>)) : <p className="text-muted-foreground">{empty}</p>;
  return (
    <>
      <PageHeader title="Product discovery insights" description={s?.disclaimer} actions={<Days value={days} onChange={setDays} />} />
      <ErrorNote error={ins.error} />
      {!s ? <Skeleton className="h-64" /> : (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Agent searches" value={s.searches} /><Stat label="Zero-result queries" value={s.zero_result_queries.length} />
            <Stat label="Few-result queries" value={s.few_result_queries.length} /><Stat label="Products requested" value={top.data?.products.length ?? "—"} />
          </div>
          <Card className="mb-4"><CardHeader><div><CardTitle className="flex items-center gap-2"><SearchX className="h-4 w-4" /> Zero-result searches</CardTitle><CardDescription>{zero.data?.note}</CardDescription></div></CardHeader>
            <CardContent className="space-y-3 text-[13px]" data-testid="zero-results">{!zero.data?.items.length ? <p className="text-muted-foreground">No zero-result searches recorded in this period.</p> : zero.data.items.map((z, i) => (
              <div key={i} className="rounded border p-2">
                <p><b>"{z.query || "(no keywords)"}"</b>{z.attribute_filters.map((a: string) => <Badge key={a} className="ml-1">{a}</Badge>)} <span className="text-muted-foreground">· {z.count} time(s) · last {timeAgo(z.last_seen)}</span></p>
                <p className="mt-1">{z.finding}</p>
                {z.affected.length > 0 && z.kind !== "no_matching_products" && <p className="mt-1 text-[12px] text-muted-foreground">Products: {z.affected.slice(0, 6).map((a: any) => <Link key={a.id} className="mr-2 hover:underline" to={`/app/catalog/${a.id}`}>{a.name}</Link>)}</p>}
              </div>))}</CardContent></Card>
          <div className="grid gap-4 lg:grid-cols-3">
            <Card><CardHeader><div><CardTitle>Most searched terms</CardTitle></div></CardHeader><CardContent className="space-y-1 text-[13px]">{list(s.top_terms.map((t: any) => ({ label: t.term, n: t.count })), "No searches.")}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Filters used</CardTitle></div></CardHeader><CardContent className="space-y-1 text-[13px]">{list(s.filters_used.map((t: any) => ({ label: humanize(t.filter), n: t.count })), "No filters used.")}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Attributes requested</CardTitle></div></CardHeader><CardContent className="space-y-1 text-[13px]">{list([...s.attributes_requested.map((t: any) => ({ label: t.attribute, n: t.count })), ...s.variant_options_requested.map((t: any) => ({ label: `option: ${t.option}`, n: t.count }))], "No attribute filters used.")}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Few-result queries</CardTitle></div></CardHeader><CardContent className="space-y-1 text-[13px]">{list(s.few_result_queries.map((t: any) => ({ label: `${t.query} (${t.results} results)`, n: t.count })), "None.")}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Requested categories</CardTitle></div></CardHeader><CardContent className="space-y-1 text-[13px]">{list([...s.categories_requested.map((t: any) => ({ label: `${t.category} (search)`, n: t.count })), ...(top.data?.categories ?? []).map((t) => ({ label: `${t.category} (product requests)`, n: t.requests }))], "None.")}</CardContent></Card>
            <Card><CardHeader><div><CardTitle>Top queries</CardTitle></div></CardHeader><CardContent className="space-y-1 text-[13px]">{list(s.top_queries.map((t: any) => ({ label: t.query, n: t.count })), "No searches.")}</CardContent></Card>
          </div>
          <Card className="mt-4 overflow-hidden"><CardHeader><div><CardTitle>Product interest signals</CardTitle><CardDescription>{top.data?.disclaimer}</CardDescription></div></CardHeader>
            {!top.data?.products.length ? <CardContent><p className="text-[13px] text-muted-foreground">No product-level requests recorded.</p></CardContent> : (
              <div className="overflow-x-auto"><Table><THead><TR><TH>Product</TH><TH className="text-right">Detail</TH><TH className="text-right">Pricing</TH><TH className="text-right">Availability</TH><TH className="text-right">Compare</TH><TH className="text-right">Variants</TH><TH className="text-right">Total</TH></TR></THead>
                <TBody>{top.data.products.map((p) => (
                  <TR key={p.product_id}><TD><Link className="hover:underline" to={`/app/catalog/${p.product_id}`}>{p.name}</Link></TD>
                    {(["product_detail", "pricing", "availability", "compare", "variants", "total"] as const).map((k) => <TD key={k} className="tabular text-right">{p[k]}</TD>)}</TR>))}</TBody></Table></div>)}
          </Card>
        </>
      )}
    </>
  );
}

/* ================================================================ Recommendations */
export function RecommendationsPage() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("open");
  const [priority, setPriority] = useState("");
  const [confirm, setConfirm] = useState<Recommendation | null>(null);
  const [first, setFirst] = useState(true);
  const recs = useQuery({ queryKey: ["intel-recs", status, priority], queryFn: async () => {
    const r = await intelligence.recommendations({ status, priority, refresh: first });
    setFirst(false);
    return r;
  } });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["intel-recs"] });
  const act = async (fn: () => Promise<unknown>, msg: string) => { try { await fn(); toast.success(msg); refresh(); } catch (e) { toast.error((e as Error).message); } };
  return (
    <>
      <PageHeader title="Recommendations" description="Rule-based, evidence-backed improvements. Nothing changes your catalog without your confirmation."
        actions={<Button variant="outline" onClick={() => act(() => intelligence.recommendations({ status, refresh: true }), "Recommendations refreshed")}><RefreshCw /> Re-analyse</Button>} />
      <div className="mb-3 flex flex-wrap gap-2">
        <NativeSelect aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="open">Open</option><option value="dismissed">Dismissed</option><option value="applied">Applied</option><option value="resolved">Resolved</option><option value="all">All</option></NativeSelect>
        <NativeSelect aria-label="Priority" value={priority} onChange={(e) => setPriority(e.target.value)}>
          <option value="">Any priority</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></NativeSelect>
      </div>
      <ErrorNote error={recs.error} />
      {recs.isLoading ? <Skeleton className="h-64" /> : !recs.data?.items.length ? <EmptyState icon={<Lightbulb />} title="No recommendations here" /> : (
        <div className="space-y-3">{recs.data.items.map((r) => (
          <Card key={r.id} data-testid="recommendation">
            <CardHeader className="flex-row flex-wrap items-start justify-between gap-2">
              <div className="min-w-0"><CardTitle className="flex flex-wrap items-center gap-2"><Badge tone={PRIORITY_TONE[r.priority]}>{humanize(r.priority)}</Badge>{r.title}</CardTitle>
                <CardDescription>{r.description}</CardDescription></div>
              <div className="flex flex-wrap gap-2">
                {r.status === "open" && r.action && <Button size="sm" onClick={() => setConfirm(r)}>{r.action === "assign_category" ? "Link categories" : "Draft descriptions for review"}</Button>}
                {r.status === "open" && <Button size="sm" variant="ghost" onClick={() => act(() => intelligence.dismiss(r.id), "Recommendation dismissed")}>Dismiss</Button>}
                {r.status === "dismissed" && <Button size="sm" variant="ghost" onClick={() => act(() => intelligence.reopen(r.id), "Reopened")}>Reopen</Button>}
              </div>
            </CardHeader>
            <CardContent className="space-y-1 text-[13px]">
              <p><b>Why:</b> {r.reason}</p>
              <p><b>Expected benefit:</b> {r.expected_benefit}</p>
              <p><b>Suggested action:</b> {r.suggested_action}</p>
              <p className="text-[12px] text-muted-foreground"><b>Priority rule:</b> {r.priority_rule}</p>
              <details><summary className="cursor-pointer text-[12px] text-muted-foreground">Affected products ({r.product_ids.length}) & evidence</summary>
                <div className="mt-1 flex flex-wrap gap-2">{(r.evidence.products ?? []).map((p) => <Link key={p.id} to={`/app/catalog/${p.id}`} className="text-[12px] text-info hover:underline">{p.name}</Link>)}</div>
                <pre className="mt-1 max-h-40 overflow-auto rounded bg-muted p-2 text-[11px]">{JSON.stringify({ ...r.evidence, products: undefined }, null, 2)}</pre></details>
            </CardContent>
          </Card>))}</div>
      )}
      <ConfirmDialog open={!!confirm} onOpenChange={(o) => !o && setConfirm(null)} title={confirm?.title ?? ""} confirmLabel="Confirm"
        description={confirm?.action === "assign_category" ? "Each listed product will be linked to the managed category matching its category text. The change is audited." :
          "Draft descriptions will be created from existing product facts. They stay pending until you approve each one in the AI assistant."}
        onConfirm={async () => { const r = await intelligence.apply(confirm!.id, { confirm: true }); toast.success(r.action === "assign_category" ? `Linked ${r.result.assigned} product(s)` : `${r.result.suggestions_created} draft(s) ready for review`); refresh(); }} />
    </>
  );
}

/* ================================================================ Anomalies */
export function AnomaliesPage() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("open");
  const [severity, setSeverity] = useState("");
  const [open, setOpen] = useState<Anomaly | null>(null);
  const [busy, setBusy] = useState(false);
  const data = useQuery({ queryKey: ["intel-anoms", status, severity], queryFn: () => intelligence.anomalies({ status, severity, refresh: status === "open" }) });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["intel-anoms"] });
  return (
    <>
      <PageHeader title="Anomaly detection" description={data.data?.note}
        actions={<Button variant="outline" loading={busy} onClick={async () => { setBusy(true); try { const r = await intelligence.checkImages(); toast.success(`${r.broken_images} unreachable image(s) found`); refresh(); } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); } }}>Check product images</Button>} />
      <div className="mb-3 flex flex-wrap gap-2">
        <NativeSelect aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="open">Open</option><option value="resolved">Resolved</option><option value="dismissed">Dismissed</option><option value="all">All</option></NativeSelect>
        <NativeSelect aria-label="Severity" value={severity} onChange={(e) => setSeverity(e.target.value)}><option value="">Any severity</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></NativeSelect>
      </div>
      <ErrorNote error={data.error} />
      <Card className="overflow-hidden">{data.isLoading ? <div className="p-4"><Skeleton className="h-40" /></div> : !data.data?.items.length ? <div className="p-6"><EmptyState icon={<AlertTriangle />} title="No anomalies" /></div> : (
        <div className="overflow-x-auto"><Table><THead><TR><TH>Severity</TH><TH>Detected</TH><TH>What</TH><TH>Product</TH><TH /></TR></THead>
          <TBody>{data.data.items.map((a) => (
            <TR key={a.id} data-testid="anomaly-row"><TD><Badge tone={SEVERITY_TONE[a.severity]}>{humanize(a.severity)}</Badge></TD>
              <TD className="whitespace-nowrap text-muted-foreground">{formatDate(a.detected_at)}</TD>
              <TD className="max-w-[340px]"><p className="font-medium">{a.title}</p><p className="text-[12px] text-muted-foreground">{a.rule_label}</p></TD>
              <TD>{a.product_id ? <Link className="hover:underline" to={`/app/catalog/${a.product_id}`}>{a.product_name}</Link> : "—"}</TD>
              <TD className="text-right"><Button size="xs" variant="outline" onClick={() => setOpen(a)}>Investigate</Button></TD></TR>))}</TBody></Table></div>)}</Card>
      <Dialog open={!!open} onOpenChange={(o) => !o && setOpen(null)}>
        <DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>{open?.title}</DialogTitle></DialogHeader>
          {open && <div className="space-y-2 text-[13px]" data-testid="anomaly-detail">
            <p><b>Why it is suspicious:</b> {open.detail}</p><p><b>Suggested investigation:</b> {open.suggestion}</p>
            <p className="text-muted-foreground">Rule: {open.rule_label} · detected {formatDate(open.detected_at)}</p>
            <pre className="max-h-60 overflow-auto rounded bg-muted p-2 text-[11px]">{JSON.stringify(open.evidence, null, 2)}</pre>
            {open.status === "open" && <Button size="sm" variant="ghost" onClick={async () => { await intelligence.dismissAnomaly(open.id); toast.success("Dismissed"); setOpen(null); refresh(); }}>Dismiss</Button>}
          </div>}
        </DialogContent></Dialog>
    </>
  );
}
