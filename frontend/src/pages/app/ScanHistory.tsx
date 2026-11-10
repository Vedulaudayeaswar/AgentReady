import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { History } from "lucide-react";
import { EmptyState, ErrorNote, PageHeader, Pagination, ScanStatusBadge } from "@/components/app/common";
import { Badge, Button, Card, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { api } from "@/services/api";
import { duration, formatDate, hostname } from "@/utils/format";

export default function ScanHistory() {
  const [page, setPage] = useState(1);
  const navigate = useNavigate();
  const { data, isLoading, error } = useQuery({
    queryKey: ["scans", page],
    queryFn: () => api.scans({ page, page_size: 20 }),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.items.some((s) => s.status === "running" || s.status === "queued") ? 3000 : false),
  });

  return (
    <>
      <PageHeader title="Scan History" description="Every scan, what it found and what changed." actions={<Button asChild size="sm"><Link to="/app/connect">New scan</Link></Button>} />
      <ErrorNote error={error} />
      <Card className="overflow-hidden">
        {isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-10" />)}</div>
        ) : !data?.items.length ? (
          <div className="p-6"><EmptyState icon={<History />} title="No scans yet" action={<Button asChild size="sm"><Link to="/app/connect">Connect website</Link></Button>} /></div>
        ) : (
          <>
            <Table>
              <THead>
                <TR className="hover:bg-transparent">
                  <TH>Started</TH><TH>Website</TH><TH>Status</TH><TH className="text-right">Pages</TH><TH className="text-right">Product pages</TH>
                  <TH className="text-right">Products</TH><TH>Changes</TH><TH className="text-right">Warnings</TH><TH className="text-right">Duration</TH>
                </TR>
              </THead>
              <TBody>
                {data.items.map((s) => {
                  const c = s.change_summary;
                  return (
                    <TR key={s.id} className="cursor-pointer" onClick={() => navigate(`/app/scans/${s.id}`)}>
                      <TD className="whitespace-nowrap text-muted-foreground">{formatDate(s.created_at)}</TD>
                      <TD>
                        <span className="font-medium">{hostname(s.website_url)}</span>
                        {s.is_rescan && <Badge className="ml-2">rescan</Badge>}
                      </TD>
                      <TD><ScanStatusBadge status={s.outcome} /></TD>
                      <TD className="tabular text-right">{s.pages_crawled}{s.pages_failed ? <span className="text-muted-foreground"> / {s.pages_failed} failed</span> : null}</TD>
                      <TD className="tabular text-right">{s.product_pages_found}</TD>
                      <TD className="tabular text-right">{s.products_extracted}</TD>
                      <TD className="text-[12px] text-muted-foreground">
                        {c.new !== undefined ? `+${c.new} new · ${c.changed ?? 0} changed · ${c.removed ?? 0} removed` : "—"}
                      </TD>
                      <TD className="tabular text-right text-muted-foreground">{s.errors.filter((e) => e.level !== "info").length}</TD>
                      <TD className="tabular text-right text-muted-foreground">{duration(s.started_at, s.finished_at)}</TD>
                    </TR>
                  );
                })}
              </TBody>
            </Table>
            <div className="border-t"><Pagination page={page} pageSize={20} total={data.total} onPage={setPage} /></div>
          </>
        )}
      </Card>
    </>
  );
}
