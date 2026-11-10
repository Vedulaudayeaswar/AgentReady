import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, Check, Circle, Loader2, X } from "lucide-react";
import { toast } from "sonner";
import { Button, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { ScanStatusBadge } from "@/components/app/common";
import { api } from "@/services/api";
import { cn } from "@/utils/cn";
import { humanize } from "@/utils/format";

const STAGE_LABELS: Record<string, string> = {
  connecting: "Connecting",
  discovering_pages: "Discovering pages",
  finding_products: "Finding products",
  extracting: "Extracting information",
  normalizing: "Normalizing attributes",
  validating: "Validating products",
  generating_catalog: "Generating catalog",
};

const ACTIVE = new Set(["queued", "running"]);

/** Polls the backend for real stage transitions and counters. No synthetic percentages. */
export function ScanProgressPanel({ scanId, compact }: { scanId: string; compact?: boolean }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["scan-progress", scanId],
    queryFn: () => api.scanProgress(scanId),
    refetchInterval: (q) => (q.state.data && !ACTIVE.has(q.state.data.status) ? false : 1500),
  });
  const status = data?.status;

  useEffect(() => {
    if (status && !ACTIVE.has(status)) {
      void qc.invalidateQueries({ queryKey: ["overview"] });
      void qc.invalidateQueries({ queryKey: ["products"] });
      void qc.invalidateQueries({ queryKey: ["scans"] });
      void qc.invalidateQueries({ queryKey: ["scan", scanId] });
    }
  }, [status, qc, scanId]);

  if (!data) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 py-6 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading scan…
        </CardContent>
      </Card>
    );
  }

  const c = data.counters;
  const counters = [
    ["Pages discovered", c.pages_discovered],
    ["Pages crawled", c.pages_crawled],
    ["Product pages", c.product_pages_found],
    ["Products extracted", c.products_extracted],
    ["AI requests", c.ai_calls],
  ] as const;

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Scan progress</CardTitle>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            {data.website_url ? <span className="font-mono text-foreground">{data.website_url}</span> : null}
            {data.website_url ? " · " : ""}Live status reported by the scan worker.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ScanStatusBadge status={data.outcome} />
          {ACTIVE.has(data.status) && (
            <Button
              variant="outline"
              size="xs"
              onClick={async () => {
                await api.cancelScan(scanId);
                toast("Cancellation requested");
              }}
            >
              <X /> Cancel
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        <ol className={cn("grid gap-2", compact ? "sm:grid-cols-2" : "sm:grid-cols-2 lg:grid-cols-4")}>
          {data.stages.map((s) => (
            <li
              key={s.key}
              className={cn(
                "flex items-center gap-2 rounded-md border px-3 py-2 text-[13px]",
                s.state === "active" && "border-info/40 bg-info/5",
                s.state === "failed" && "border-destructive/40 bg-destructive/5",
                s.state === "pending" && "text-muted-foreground",
              )}
            >
              {s.state === "done" && <Check className="h-3.5 w-3.5 text-success" />}
              {s.state === "active" && <Loader2 className="h-3.5 w-3.5 animate-spin text-info" />}
              {s.state === "failed" && <AlertTriangle className="h-3.5 w-3.5 text-destructive" />}
              {s.state === "pending" && <Circle className="h-3.5 w-3.5" />}
              {STAGE_LABELS[s.key] ?? humanize(s.key)}
            </li>
          ))}
        </ol>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {counters.map(([label, v]) => (
            <div key={label}>
              <dt className="text-[11.5px] text-muted-foreground">{label}</dt>
              <dd className="tabular text-lg font-semibold">{v ?? 0}</dd>
            </div>
          ))}
        </dl>
        {data.errors.length > 0 && (
          <div className="space-y-1 rounded-md border bg-muted/30 p-3">
            <p className="text-[12px] font-medium text-muted-foreground">Recent messages</p>
            {data.errors.slice(-5).map((e, i) => (
              <p
                key={i}
                className={cn("truncate font-mono text-[11.5px]", e.level === "info" ? "text-muted-foreground" : e.level === "warning" ? "text-warning" : "text-destructive")}
                title={e.message}
              >
                [{e.stage}] {e.message} {e.url ? `· ${e.url}` : ""}
              </p>
            ))}
          </div>
        )}
        {data.status === "awaiting_review" && (
          <div className="flex items-center justify-between rounded-md border border-warning/30 bg-warning/5 px-3 py-2.5 text-[13px]">
            <span>This rescan found changes. Review them before they are applied to your catalog.</span>
            <Button asChild size="sm"><Link to={`/app/scans/${scanId}`}>Review changes</Link></Button>
          </div>
        )}
        {data.status === "completed" && !compact && (
          <div className="flex justify-end">
            <Button asChild size="sm"><Link to="/app/catalog">Open catalog</Link></Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
