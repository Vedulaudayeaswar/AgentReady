import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { ArrowRight, Download, Globe, History, Package } from "lucide-react";
import { EmptyState, ErrorNote, PageHeader, ScanStatusBadge, Stat } from "@/components/app/common";
import { ScanProgressPanel } from "@/components/app/ScanProgressPanel";
import { CommerceCharts, CommerceStats } from "@/components/app/CommerceDashboard";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Skeleton } from "@/components/ui/primitives";
import { api } from "@/services/api";
import { bytes, hostname, humanize, timeAgo } from "@/utils/format";

const QUALITY_ROWS = [
  { key: "valid", label: "Valid", color: "hsl(var(--success))" },
  { key: "needs_review", label: "Needs review", color: "hsl(var(--warning))" },
  { key: "incomplete", label: "Incomplete", color: "hsl(var(--muted-foreground))" },
  { key: "duplicate_candidate", label: "Possible duplicate", color: "hsl(var(--info))" },
  { key: "invalid", label: "Invalid", color: "hsl(var(--destructive))" },
] as const;

export default function Overview() {
  const { data, isLoading, error } = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 20_000 });

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <div className="grid gap-3 sm:grid-cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24" />)}</div>
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (error || !data) return <ErrorNote error={error} />;

  if (data.websites === 0 && data.total_products === 0) {
    return (
      <>
        <PageHeader title="Overview" />
        <EmptyState
          icon={<Globe />}
          title="Connect your first website"
          description="AgentReady will discover product pages, extract product data and build a validated catalog you can review. You can also create products manually or connect a store."
          action={<div className="flex flex-wrap justify-center gap-2"><Button asChild><Link to="/app/connect">Connect website <ArrowRight /></Link></Button>
            <Button asChild variant="outline"><Link to="/app/catalog/new">Create a product</Link></Button>
            <Button asChild variant="outline"><Link to="/app/integrations">Connect a store</Link></Button></div>}
        />
      </>
    );
  }

  const total = data.total_products;
  const scanActive = data.latest_scan && ["queued", "running"].includes(data.latest_scan.status);
  const reviewNeeded = data.quality.needs_review + data.quality.incomplete + data.quality.duplicate_candidate + data.quality.invalid;

  return (
    <>
      <PageHeader
        title="Overview"
        description="All figures are computed from your catalog data."
        actions={<Button asChild size="sm"><Link to="/app/connect"><Globe /> Scan website</Link></Button>}
      />

      {scanActive && data.latest_scan && (
        <div className="mb-6"><ScanProgressPanel scanId={data.latest_scan.id} /></div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Total products" value={total} hint={`${data.approval.approved} approved`} />
        <Stat label="Valid products" value={data.quality.valid} hint={total ? `${Math.round((data.quality.valid / total) * 100)}% of catalog` : undefined} />
        <Stat label="Need attention" value={reviewNeeded} hint={`${data.quality.incomplete} incomplete`} />
        <Stat
          label="Catalog completeness"
          value={data.catalog_completeness !== null ? `${data.catalog_completeness}%` : "—"}
          hint="Average share of key fields present"
        />
      </div>

      {data.commerce && (
        <>
          <h2 className="mb-3 mt-6 text-sm font-semibold">Commerce</h2>
          <CommerceStats c={data.commerce} />
          <CommerceCharts />
        </>
      )}

      <div className="mt-6 grid gap-4 lg:grid-cols-[1.3fr_1fr]">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Product quality</CardTitle>
              <CardDescription>Status after validation</CardDescription>
            </div>
            <Button asChild variant="ghost" size="xs"><Link to="/app/validation">Validation center <ArrowRight /></Link></Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {total === 0 ? (
              <p className="text-[13px] text-muted-foreground">No products yet.</p>
            ) : (
              <>
                <div className="flex h-2.5 overflow-hidden rounded-full bg-muted">
                  {QUALITY_ROWS.map((r) => {
                    const n = data.quality[r.key];
                    return n ? <div key={r.key} style={{ width: `${(n / total) * 100}%`, background: r.color }} title={`${r.label}: ${n}`} /> : null;
                  })}
                </div>
                <ul className="grid gap-1.5 sm:grid-cols-2">
                  {QUALITY_ROWS.map((r) => (
                    <li key={r.key}>
                      <Link to={`/app/catalog?quality_status=${r.key}`} className="flex items-center justify-between rounded-md px-2 py-1.5 text-[13px] hover:bg-accent">
                        <span className="flex items-center gap-2">
                          <span className="h-2 w-2 rounded-full" style={{ background: r.color }} />
                          {r.label}
                        </span>
                        <span className="tabular text-muted-foreground">{data.quality[r.key]}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
                <p className="text-[12px] text-muted-foreground">
                  Open issues: {data.open_issues.error} errors · {data.open_issues.warning} warnings · {data.open_issues.info} notes
                </p>
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Top categories</CardTitle>
              <CardDescription>Products per category</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="h-[220px]">
            {data.categories.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">No categories yet.</p>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.categories} layout="vertical" margin={{ left: 0, right: 12, top: 0, bottom: 0 }}>
                  <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
                  <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="category" width={120} tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }} axisLine={false} tickLine={false} />
                  <RTooltip
                    cursor={{ fill: "hsl(var(--muted))" }}
                    contentStyle={{ background: "hsl(var(--popover))", border: "1px solid hsl(var(--border))", borderRadius: 6, fontSize: 12 }}
                  />
                  <Bar dataKey="count" fill="hsl(var(--foreground))" radius={[0, 3, 3, 0]} barSize={14} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><History className="h-4 w-4 text-muted-foreground" />Latest scan</CardTitle>
          </CardHeader>
          <CardContent>
            {data.latest_scan ? (
              <div className="space-y-2 text-[13px]">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{hostname(data.latest_scan.website_url)}</span>
                  <ScanStatusBadge status={data.latest_scan.outcome} />
                </div>
                <p className="text-muted-foreground">
                  {data.latest_scan.pages_crawled} pages · {data.latest_scan.products_extracted} products · {timeAgo(data.latest_scan.created_at)}
                </p>
                <Button asChild variant="outline" size="xs"><Link to={`/app/scans/${data.latest_scan.id}`}>View scan</Link></Button>
              </div>
            ) : (
              <p className="text-[13px] text-muted-foreground">No scans yet.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Package className="h-4 w-4 text-muted-foreground" />Recent changes</CardTitle>
          </CardHeader>
          <CardContent>
            {data.recent_changes.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">Changes found by rescanning a website appear here.</p>
            ) : (
              <ul className="space-y-2">
                {data.recent_changes.slice(0, 6).map((c) => (
                  <li key={c.id} className="flex min-w-0 items-center justify-between gap-2 text-[13px]">
                    <span className="min-w-0 truncate" title={c.name ?? undefined}>{c.name ?? "Unnamed product"}</span>
                    <Badge tone={c.change_type === "new" ? "success" : c.change_type === "removed" ? "danger" : "warning"}>{humanize(c.change_type)}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Download className="h-4 w-4 text-muted-foreground" />Export history</CardTitle>
          </CardHeader>
          <CardContent>
            {data.recent_exports.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">No exports yet.</p>
            ) : (
              <ul className="space-y-2">
                {data.recent_exports.map((e) => (
                  <li key={e.id} className="flex items-center justify-between text-[13px]">
                    <span className="font-mono text-[12px] uppercase">{e.format}</span>
                    <span className="text-muted-foreground">{e.product_count} products · {bytes(e.size_bytes)} · {timeAgo(e.created_at)}</span>
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
