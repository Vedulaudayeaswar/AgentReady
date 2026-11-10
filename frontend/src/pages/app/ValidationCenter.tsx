import { useState } from "react";
import { Link } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CheckCircle2, RotateCcw, X } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader, Pagination, QualityBadge, SeverityBadge } from "@/components/app/common";
import { Badge, Button, Card, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { api } from "@/services/api";
import { cn } from "@/utils/cn";
import { humanize } from "@/utils/format";

const GROUPS = [
  { key: "", label: "All issues" },
  { key: "missing_price,missing_name,missing_currency,missing_source", label: "Incomplete" },
  { key: "invalid_price,invalid_currency,invalid_gtin,invalid_name,invalid_url,invalid_availability,invalid_image_url,invalid_sku", label: "Invalid data" },
  { key: "possible_duplicate", label: "Duplicates" },
  { key: "conflicting_values,conflicting_attribute,unverified_ai_evidence,ambiguous_field", label: "Conflicts & AI review" },
];

export default function ValidationCenter() {
  const qc = useQueryClient();
  const [group, setGroup] = useState("");
  const [severity, setSeverity] = useState("error,warning");
  const [status, setStatus] = useState("open");
  const [page, setPage] = useState(1);
  const summary = useQuery({ queryKey: ["issue-summary"], queryFn: api.issueSummary });
  const issues = useQuery({
    queryKey: ["issues", { group, severity, status, page }],
    queryFn: () => api.issues({ code: group || undefined, severity: severity || undefined, status, page, page_size: 50 }),
    placeholderData: keepPreviousData,
  });

  const count = (codes: string) =>
    (summary.data?.items ?? []).filter((i) => (!codes || codes.split(",").includes(i.code)) && (!severity || severity.split(",").includes(i.severity))).reduce((a, b) => a + b.count, 0);

  const act = async (id: string, action: "resolve" | "dismiss" | "reopen") => {
    await api.resolveIssue(id, action);
    toast.success(action === "reopen" ? "Reopened" : action === "resolve" ? "Resolved" : "Dismissed");
    void qc.invalidateQueries({ queryKey: ["issues"] });
    void qc.invalidateQueries({ queryKey: ["issue-summary"] });
    void qc.invalidateQueries({ queryKey: ["overview"] });
    void qc.invalidateQueries({ queryKey: ["products"] });
  };

  return (
    <>
      <PageHeader
        title="Validation Center"
        description="Issues are recomputed whenever products change. Fixing the underlying data resolves them automatically; you can also resolve or dismiss them."
      />
      <div className="mb-4 flex flex-wrap gap-2">
        {GROUPS.map((g) => (
          <button
            key={g.label}
            onClick={() => {
              setGroup(g.key);
              setPage(1);
            }}
            className={cn(
              "flex items-center gap-2 rounded-md border px-3 py-1.5 text-[13px] transition-colors hover:bg-accent",
              group === g.key && "border-foreground/30 bg-accent font-medium",
            )}
          >
            {g.label}
            <span className="tabular text-[11.5px] text-muted-foreground">{count(g.key)}</span>
          </button>
        ))}
        <div className="flex-1" />
        <NativeSelect value={severity} onChange={(e) => { setSeverity(e.target.value); setPage(1); }} aria-label="Severity">
          <option value="error,warning">Errors & warnings</option>
          <option value="error">Errors only</option>
          <option value="warning">Warnings only</option>
          <option value="info">Notes</option>
          <option value="">All severities</option>
        </NativeSelect>
        <NativeSelect value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} aria-label="Status">
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
          <option value="dismissed">Dismissed</option>
          <option value="all">All</option>
        </NativeSelect>
      </div>

      <ErrorNote error={issues.error} />
      <Card className="overflow-hidden">
        {issues.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10" />)}</div>
        ) : !issues.data?.items.length ? (
          <div className="p-6"><EmptyState icon={<CheckCircle2 />} title="Nothing to review" description="No issues match these filters." /></div>
        ) : (
          <>
            <Table>
              <THead><TR className="hover:bg-transparent"><TH>Severity</TH><TH>Issue</TH><TH>Product</TH><TH>Field</TH><TH>Status</TH><TH className="text-right">Actions</TH></TR></THead>
              <TBody>
                {issues.data.items.map((i) => (
                  <TR key={i.id} className="align-top">
                    <TD><SeverityBadge severity={i.severity} /></TD>
                    <TD className="max-w-[380px]">
                      <p className="text-[13px]">{i.message}</p>
                      <p className="font-mono text-[11.5px] text-muted-foreground">{i.code}</p>
                      {Array.isArray(i.details.values) && <p className="font-mono text-[11.5px] text-muted-foreground">{(i.details.values as string[]).join(" vs ")}</p>}
                    </TD>
                    <TD className="max-w-[220px]">
                      {i.product && (
                        <Link to={`/app/catalog/${i.product.id}`} className="block hover:underline">
                          <span className="block truncate font-medium">{i.product.name ?? "Unnamed"}</span>
                        </Link>
                      )}
                      {i.product && <QualityBadge status={i.product.quality_status} />}
                    </TD>
                    <TD className="font-mono text-[12px] text-muted-foreground">{i.field?.startsWith("duplicate:") ? "duplicate" : i.field ?? "—"}</TD>
                    <TD><Badge>{humanize(i.status)}</Badge></TD>
                    <TD className="text-right">
                      <div className="flex justify-end gap-1">
                        {i.status === "open" ? (
                          <>
                            <Button size="xs" variant="outline" asChild><Link to={`/app/catalog/${i.product_id}`}>Fix</Link></Button>
                            <Button size="xs" variant="ghost" onClick={() => act(i.id, "resolve")} aria-label="Resolve"><Check /></Button>
                            <Button size="xs" variant="ghost" onClick={() => act(i.id, "dismiss")} aria-label="Dismiss"><X /></Button>
                          </>
                        ) : (
                          <Button size="xs" variant="ghost" onClick={() => act(i.id, "reopen")}><RotateCcw /> Reopen</Button>
                        )}
                      </div>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
            <div className="border-t"><Pagination page={page} pageSize={50} total={issues.data.total} onPage={setPage} /></div>
          </>
        )}
      </Card>
    </>
  );
}
