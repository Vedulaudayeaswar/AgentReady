import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { Activity, ArrowRight, Boxes } from "lucide-react";
import { Stat } from "@/components/app/common";
import { StockBadge } from "@/components/app/commerce";
import { Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Skeleton } from "@/components/ui/primitives";
import { commerce } from "@/services/commerce";
import type { CommerceOverview } from "@/types/commerce";
import { humanize, timeAgo } from "@/utils/format";

const axis = { fontSize: 11, fill: "hsl(var(--muted-foreground))" };
const tip = { background: "hsl(var(--popover))", border: "1px solid hsl(var(--border))", borderRadius: 6, fontSize: 12 };
const STOCK_COLORS: Record<string, string> = {
  in_stock: "hsl(var(--success))", low_stock: "hsl(var(--warning))", out_of_stock: "hsl(var(--destructive))",
  backorder: "hsl(var(--info))", untracked: "hsl(var(--muted-foreground))",
};
const short = (d: string) => d.slice(5);

export function CommerceStats({ c }: { c: CommerceOverview }) {
  const tiles: [string, number, string, string?][] = [
    ["Active", c.active, "/app/catalog?lifecycle_status=active"],
    ["Draft", c.draft, "/app/catalog?lifecycle_status=draft"],
    ["Approved", c.approved, "/app/catalog?approval_status=approved"],
    ["Published", c.published, "/app/catalog?publication_status=published", "in catalog API & feeds"],
    ["Archived", c.archived, "/app/catalog?lifecycle_status=archived"],
    ["Low stock", c.low_stock, "/app/inventory"],
    ["Out of stock", c.out_of_stock, "/app/catalog?stock_status=out_of_stock,backorder"],
    ["Incomplete info", c.incomplete, "/app/catalog?quality_status=incomplete,invalid"],
    ["Pending website changes", c.pending_website_changes, "/app/scans"],
    ["Pending sync operations", c.pending_sync_operations, "/app/sync",
      `${c.pending.open_conflicts} conflicts · ${c.pending.scheduled_price_changes} scheduled prices`],
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {tiles.map(([label, value, to, hint]) => (
        <Link key={label} to={to}><Stat label={label} value={value} hint={hint} className="h-full hover:bg-accent/40" /></Link>
      ))}
    </div>
  );
}

export function CommerceCharts() {
  const charts = useQuery({ queryKey: ["charts"], queryFn: () => commerce.charts(30) });
  const low = useQuery({ queryKey: ["low-stock"], queryFn: commerce.lowStock });
  const activity = useQuery({ queryKey: ["activity"], queryFn: commerce.activity, refetchInterval: 30_000 });
  if (charts.isLoading) return <div className="mt-4 grid gap-4 lg:grid-cols-2"><Skeleton className="h-64" /><Skeleton className="h-64" /></div>;
  const d = charts.data;
  if (!d) return null;
  const stock = d.inventory_status.filter((s) => s.count > 0);
  const anyPrice = d.price_changes.some((x) => x.manual + x.bulk + x.scheduled + x.sync > 0);
  const anySync = d.sync_activity.some((x) => x.succeeded + x.failed + x.review > 0);
  return (
    <>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><div><CardTitle>Catalog growth</CardTitle><CardDescription>Products in the catalog, last {d.days} days</CardDescription></div></CardHeader>
          <CardContent className="h-[220px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={d.catalog_growth} margin={{ left: -16, right: 8, top: 4 }}>
                <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
                <XAxis dataKey="day" tickFormatter={short} tick={axis} axisLine={false} tickLine={false} minTickGap={24} />
                <YAxis allowDecimals={false} tick={axis} axisLine={false} tickLine={false} />
                <RTooltip contentStyle={tip} />
                <Area type="monotone" dataKey="products" stroke="hsl(var(--foreground))" fill="hsl(var(--foreground) / 0.08)" strokeWidth={1.5} name="Products" />
              </AreaChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><CardTitle>Inventory status</CardTitle><CardDescription>Products (excluding archived)</CardDescription></div></CardHeader>
          <CardContent className="h-[220px]">
            {stock.length === 0 ? <p className="text-[13px] text-muted-foreground">No products yet.</p> : (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={stock.map((s) => ({ ...s, label: humanize(s.status === "untracked" ? "not_tracked" : s.status) }))} dataKey="count" nameKey="label" innerRadius={50} outerRadius={80} paddingAngle={2}>
                    {stock.map((s) => <Cell key={s.status} fill={STOCK_COLORS[s.status]} />)}
                  </Pie>
                  <Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                  <RTooltip contentStyle={tip} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><CardTitle>Price-change activity</CardTitle><CardDescription>Recorded price changes per day</CardDescription></div></CardHeader>
          <CardContent className="h-[220px]">
            {!anyPrice ? <p className="text-[13px] text-muted-foreground">No price changes in this period.</p> : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={d.price_changes} margin={{ left: -16, right: 8, top: 4 }}>
                  <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
                  <XAxis dataKey="day" tickFormatter={short} tick={axis} axisLine={false} tickLine={false} minTickGap={24} />
                  <YAxis allowDecimals={false} tick={axis} axisLine={false} tickLine={false} />
                  <RTooltip contentStyle={tip} cursor={{ fill: "hsl(var(--muted))" }} />
                  <Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="manual" stackId="p" fill="hsl(var(--foreground))" name="Manual" />
                  <Bar dataKey="bulk" stackId="p" fill="hsl(var(--info))" name="Bulk" />
                  <Bar dataKey="scheduled" stackId="p" fill="hsl(var(--warning))" name="Scheduled" />
                  <Bar dataKey="sync" stackId="p" fill="hsl(var(--success))" name="Sync" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><CardTitle>Synchronization activity</CardTitle><CardDescription>Website and store syncs per day</CardDescription></div></CardHeader>
          <CardContent className="h-[220px]">
            {!anySync ? <p className="text-[13px] text-muted-foreground">No syncs in this period.</p> : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={d.sync_activity} margin={{ left: -16, right: 8, top: 4 }}>
                  <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
                  <XAxis dataKey="day" tickFormatter={short} tick={axis} axisLine={false} tickLine={false} minTickGap={24} />
                  <YAxis allowDecimals={false} tick={axis} axisLine={false} tickLine={false} />
                  <RTooltip contentStyle={tip} cursor={{ fill: "hsl(var(--muted))" }} />
                  <Legend iconSize={8} wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="succeeded" stackId="s" fill="hsl(var(--success))" name="Succeeded" />
                  <Bar dataKey="review" stackId="s" fill="hsl(var(--warning))" name="Awaiting review" />
                  <Bar dataKey="failed" stackId="s" fill="hsl(var(--destructive))" name="Failed" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_1.4fr]">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Boxes className="h-4 w-4 text-muted-foreground" />Low stock</CardTitle>
            <Button asChild variant="ghost" size="xs"><Link to="/app/inventory">Inventory <ArrowRight /></Link></Button>
          </CardHeader>
          <CardContent>
            {!low.data?.items.length ? <p className="text-[13px] text-muted-foreground">Nothing low on stock.</p> : (
              <ul className="space-y-2">
                {low.data.items.slice(0, 6).map((i) => (
                  <li key={`${i.product_id}${i.variant_id}`} className="flex min-w-0 items-center justify-between gap-2 text-[13px]">
                    <Link to={`/app/catalog/${i.product_id}`} className="min-w-0 truncate hover:underline">{i.name}</Link>
                    <StockBadge status={i.stock_status} available={i.available} />
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Activity className="h-4 w-4 text-muted-foreground" />Activity</CardTitle>
            <Button asChild variant="ghost" size="xs"><Link to="/app/audit">Audit log <ArrowRight /></Link></Button>
          </CardHeader>
          <CardContent>
            {!activity.data?.items.length ? <p className="text-[13px] text-muted-foreground">No activity yet.</p> : (
              <ul className="space-y-2">
                {activity.data.items.slice(0, 8).map((a) => (
                  <li key={a.id} className="flex min-w-0 items-start justify-between gap-3 text-[13px]">
                    <span className="min-w-0 truncate" title={a.summary}>{a.summary}</span>
                    <span className="shrink-0 text-[12px] text-muted-foreground">{timeAgo(a.created_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
