import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ExternalLink, Lock } from "lucide-react";
import { toast } from "sonner";
import { ErrorNote, MethodBadge, PageHeader, ScanStatusBadge, Stat } from "@/components/app/common";
import { ScanProgressPanel } from "@/components/app/ScanProgressPanel";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { api } from "@/services/api";
import type { SnapshotChange } from "@/types/api";
import { displayValue, duration, formatDate, hostname, humanize, path } from "@/utils/format";

const SOURCE_LABEL: Record<string, string> = {
  json_ld: "JSON-LD structured data", microdata: "Microdata", meta: "Meta tags", html: "Page HTML",
  html_table: "Product tables", html_spec: "Specification tables", ai: "AI extraction",
};
const FIELD_LABEL: Record<string, string> = { price_amount: "Price", attributes: "Specifications", _removed: "Product" };

export default function ScanDetail() {
  const { scanId = "" } = useParams();
  const qc = useQueryClient();
  const scan = useQuery({
    queryKey: ["scan", scanId],
    queryFn: () => api.scan(scanId),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 2000 : false),
  });
  const s = scan.data;
  const done = s && !["queued", "running"].includes(s.status);
  const changes = useQuery({ queryKey: ["scan-changes", scanId], queryFn: () => api.scanChanges(scanId), enabled: !!done });
  const pages = useQuery({ queryKey: ["scan-pages", scanId], queryFn: () => api.scanPages(scanId), enabled: !!done });
  const [deselected, setDeselected] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);

  const pending = useMemo(() => (changes.data?.items ?? []).filter((c) => c.decision === "pending"), [changes.data]);

  if (scan.isLoading) return <Skeleton className="h-64" />;
  if (scan.error || !s) return <ErrorNote error={scan.error ?? "Scan not found"} />;

  const apply = async () => {
    setApplying(true);
    try {
      const ids = pending.filter((c) => !deselected.has(c.id)).map((c) => c.id);
      const r = await api.applyScan(scanId, ids);
      toast.success("Changes applied", {
        description: `${r.applied.created} created · ${r.applied.updated} updated · ${r.applied.removed} removed · ${r.applied.skipped} skipped`,
      });
      ["scan", "scan-changes", "products", "overview", "scans", "issues"].forEach((k) => void qc.invalidateQueries({ queryKey: [k] }));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setApplying(false);
    }
  };

  const c = s.change_summary;
  return (
    <>
      <Link to="/app/scans" className="mb-4 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground"><ArrowLeft className="h-3.5 w-3.5" /> Scan history</Link>
      <PageHeader
        title={`Scan · ${hostname(s.website_url)}`}
        description={<>Started {formatDate(s.started_at ?? s.created_at)} · {duration(s.started_at, s.finished_at)} {s.is_rescan && <Badge className="ml-1">rescan</Badge>}</>}
        actions={<ScanStatusBadge status={s.outcome} />}
      />

      {!done ? (
        <ScanProgressPanel scanId={scanId} />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Stat label="Pages crawled" value={s.pages_crawled} hint={`${s.pages_discovered} discovered · ${s.pages_skipped_robots} blocked by robots.txt`} />
            <Stat label="Product pages" value={s.product_pages_found} />
            <Stat label="Products extracted" value={s.products_extracted} />
            <Stat label="AI requests" value={s.ai_calls} />
            <Stat label="New / changed / removed" value={`${c.new ?? 0} / ${c.changed ?? 0} / ${c.removed ?? 0}`} />
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>How products were extracted</CardTitle>
                  <CardDescription>Products with evidence from each method in this scan (a product can use several).</CardDescription>
                </div>
              </CardHeader>
              <CardContent>
                {s.extraction_methods && s.extraction_methods.length > 0 ? (
                  <ul className="space-y-1.5 text-[13px]">
                    {[...s.extraction_methods].sort((a, b) => b.products - a.products).map((m) => (
                      <li key={`${m.method}-${m.source_type}`} className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-2">
                          <MethodBadge method={m.method} model={s.ai?.models[0]} />
                          <span className="text-muted-foreground">{SOURCE_LABEL[m.source_type] ?? humanize(m.source_type)}</span>
                        </span>
                        <span className="tabular">{m.products}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-[13px] text-muted-foreground">
                    {s.status === "awaiting_review" ? "Evidence is recorded when changes are applied." : "No evidence recorded for this scan."}
                  </p>
                )}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>AI usage in this scan</CardTitle>
                  <CardDescription>Groq is only called when deterministic extraction is insufficient.</CardDescription>
                </div>
              </CardHeader>
              <CardContent className="text-[13px]">
                {!s.ai || s.ai.requests === 0 ? (
                  <p className="text-muted-foreground">AI was not used in this scan. Every value came from deterministic extraction.</p>
                ) : (
                  <dl className="grid grid-cols-2 gap-y-1.5">
                    <dt className="text-muted-foreground">Model</dt>
                    <dd className="font-mono text-[12px]">{s.ai.models.join(", ")}</dd>
                    <dt className="text-muted-foreground">Requests</dt>
                    <dd className="tabular">{s.ai.succeeded} succeeded / {s.ai.cached} cached / {s.ai.failed} failed</dd>
                    <dt className="text-muted-foreground">Tokens</dt>
                    <dd className="tabular">{s.ai.tokens.toLocaleString()}</dd>
                    <dt className="text-muted-foreground">Estimated cost</dt>
                    <dd className="tabular">${s.ai.estimated_cost_usd.toFixed(4)}</dd>
                  </dl>
                )}
              </CardContent>
            </Card>
          </div>

          <Tabs defaultValue={s.status === "awaiting_review" ? "changes" : "pages"} className="mt-6">
            <TabsList>
              <TabsTrigger value="changes">Change report {pending.length > 0 && <Badge tone="warning" className="ml-1">{pending.length} pending</Badge>}</TabsTrigger>
              <TabsTrigger value="pages">Pages <Badge className="ml-1">{pages.data ? pages.data.items.length : "..."}</Badge></TabsTrigger>
              <TabsTrigger value="errors">Messages <Badge className="ml-1">{s.errors.length}</Badge></TabsTrigger>
            </TabsList>

            <TabsContent value="changes">
              {s.status === "awaiting_review" && (
                <Card className="mb-4 border-warning/40">
                  <CardHeader>
                    <div>
                      <CardTitle>Review before applying</CardTitle>
                      <CardDescription>
                        Nothing in your catalog has changed yet. Untick anything you do not want applied. Fields you edited
                        (<Lock className="inline h-3 w-3" />) are always kept.
                      </CardDescription>
                    </div>
                    <Button onClick={apply} loading={applying} disabled={changes.isLoading || pending.length === 0}>
                      <Check /> {changes.isLoading ? "Loading changes..." : `Apply ${pending.length - deselected.size} change(s)`}
                    </Button>
                  </CardHeader>
                  <CardContent className="flex flex-wrap gap-2 text-[12.5px] text-muted-foreground">
                    <span>{c.price_changes ?? 0} price changes</span>·<span>{c.description_changes ?? 0} description changes</span>·
                    <span>{c.specification_changes ?? 0} specification changes</span>·<span>{c.availability_changes ?? 0} availability changes</span>
                  </CardContent>
                </Card>
              )}
              {changes.isLoading ? (
                <div className="space-y-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-12" />)}</div>
              ) : changes.error ? (
                <ErrorNote error={changes.error} />
              ) : (
              <ChangeReport
                items={changes.data?.items ?? []}
                selectable={s.status === "awaiting_review"}
                deselected={deselected}
                onToggle={(id) => {
                  const next = new Set(deselected);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  setDeselected(next);
                }}
              />
              )}
            </TabsContent>

            <TabsContent value="pages">
              {pages.isLoading ? <Skeleton className="h-40" /> : (
              <Card className="overflow-hidden">
                <Table>
                  <THead><TR className="hover:bg-transparent"><TH>Page</TH><TH>Type</TH><TH className="text-right">Score</TH><TH>Status</TH><TH>Signals</TH></TR></THead>
                  <TBody>
                    {(pages.data?.items ?? []).map((p) => (
                      <TR key={p.id} className="align-top">
                        <TD className="max-w-[320px]">
                          <a href={p.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-mono text-[12px] hover:underline">
                            {path(p.url)} <ExternalLink className="h-3 w-3" />
                          </a>
                          {p.title && <p className="truncate text-[12px] text-muted-foreground">{p.title}</p>}
                        </TD>
                        <TD><Badge tone={p.page_type === "product" ? "success" : p.page_type === "listing" ? "info" : "neutral"}>{p.page_type}</Badge></TD>
                        <TD className="tabular text-right">{p.product_score}</TD>
                        <TD className="text-[12px] text-muted-foreground">{p.error ?? p.status_code ?? "—"}{p.render_mode !== "http" && ` · ${p.render_mode}`}</TD>
                        <TD className="max-w-[320px]">
                          <details>
                            <summary className="cursor-pointer text-[12px] text-muted-foreground">
                              {(p.signals.reasons ?? []).length} signal(s){p.signals.used_ai ? " · AI used" : ""}{p.signals.products !== undefined ? ` · ${p.signals.products} product(s)` : ""}
                            </summary>
                            <ul className="mt-1 font-mono text-[11px] text-muted-foreground">{(p.signals.reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}</ul>
                          </details>
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </Card>
              )}
            </TabsContent>

            <TabsContent value="errors">
              {s.errors.length === 0 ? (
                <p className="text-[13px] text-muted-foreground">No errors or warnings recorded.</p>
              ) : (
                <Card className="overflow-hidden">
                  <Table>
                    <THead><TR className="hover:bg-transparent"><TH>Level</TH><TH>Stage</TH><TH>Message</TH><TH>URL</TH></TR></THead>
                    <TBody>
                      {s.errors.map((e, i) => (
                        <TR key={i}>
                          <TD><Badge tone={e.level === "info" ? "neutral" : e.level === "warning" ? "warning" : "danger"}>{e.level ?? "error"}</Badge></TD>
                          <TD className="whitespace-nowrap text-muted-foreground">{humanize(e.stage)}</TD>
                          <TD className="text-[13px]">{e.message}</TD>
                          <TD className="font-mono text-[11.5px] text-muted-foreground">{e.url ? path(e.url) : "—"}</TD>
                        </TR>
                      ))}
                    </TBody>
                  </Table>
                </Card>
              )}
            </TabsContent>
          </Tabs>
        </>
      )}
    </>
  );
}

function ChangeReport({ items, selectable, deselected, onToggle }: { items: SnapshotChange[]; selectable: boolean; deselected: Set<string>; onToggle: (id: string) => void }) {
  if (!items.length) return <p className="text-[13px] text-muted-foreground">No new, changed or removed products in this scan.</p>;
  const tone = (t: string) => (t === "new" ? "success" : t === "removed" ? "danger" : "warning") as "success" | "danger" | "warning";
  return (
    <Card className="overflow-hidden">
      <Table>
        <THead><TR className="hover:bg-transparent">{selectable && <TH className="w-8" />}<TH>Change</TH><TH>Product</TH><TH>Details</TH><TH>Decision</TH></TR></THead>
        <TBody>
          {items.map((c) => (
            <TR key={c.id} className="align-top">
              {selectable && (
                <TD>{c.decision === "pending" && <Checkbox checked={!deselected.has(c.id)} onCheckedChange={() => onToggle(c.id)} aria-label="Apply this change" />}</TD>
              )}
              <TD><Badge tone={tone(c.change_type)}>{humanize(c.change_type)}</Badge></TD>
              <TD className="max-w-[220px]">
                {c.product_id ? <Link to={`/app/catalog/${c.product_id}`} className="font-medium hover:underline">{c.name ?? "Unnamed"}</Link> : <span className="font-medium">{c.name ?? "Unnamed"}</span>}
                {c.source_url && <p className="truncate font-mono text-[11px] text-muted-foreground">{path(c.source_url)}</p>}
              </TD>
              <TD className="max-w-[460px]">
                {c.change_type === "new" ? (
                  <span className="text-[12.5px] text-muted-foreground">New product · {displayValue(c.price)}</span>
                ) : (
                  <ul className="space-y-1 text-[12.5px]">
                    {c.changes.map((f, i) => (
                      <li key={i} className="flex flex-wrap items-baseline gap-1.5">
                        <span className="font-medium">{FIELD_LABEL[f.field] ?? humanize(f.field)}</span>
                        {f.field === "_removed" ? (
                          <span className="text-muted-foreground">{f.reason}</span>
                        ) : (
                          <>
                            <span className="break-all text-muted-foreground line-through">{displayValue(f.old).slice(0, 120)}</span>
                            <span>→</span>
                            <span className="break-all">{displayValue(f.new).slice(0, 120)}</span>
                          </>
                        )}
                        {f.manual_override && <Badge tone="solid"><Lock className="h-3 w-3" /> your edit kept</Badge>}
                      </li>
                    ))}
                  </ul>
                )}
              </TD>
              <TD><Badge>{humanize(c.decision)}</Badge></TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </Card>
  );
}
