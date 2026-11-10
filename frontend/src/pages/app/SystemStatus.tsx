import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity, CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { EmptyState, ErrorNote, PageHeader } from "@/components/app/common";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { request } from "@/services/api";
import { formatDate, humanize } from "@/utils/format";

export type CheckStatus = "ok" | "error" | "not_used";
export interface HealthCheck { status: CheckStatus; latency_ms?: number; error?: string; detail?: string; workers?: number; last_tick_at?: string; age_seconds?: number; cached?: boolean }
export interface SystemStatus {
  health: { status: "ok" | "degraded" | "unavailable"; environment: string; ai_provider: string; ai_configured: boolean; job_backend: string;
    checks: Record<string, HealthCheck>; checked_at: string };
  period_days: number;
  jobs: Record<string, Record<string, number>>;
  active_jobs: number;
  recent_failures: { kind: string; id: string; status: string; name?: string; at: string | null; error: string; detail?: string; retry: string; link: string }[];
}

export const OVERALL_TONE = { ok: "success", degraded: "warning", unavailable: "danger" } as const;
export const CHECK_LABELS: Record<string, string> = {
  database: "Database (PostgreSQL)", redis: "Job queue (Redis)", worker: "Background worker (Celery)", scheduler: "Scheduler (Celery Beat)",
};
const JOB_LABELS: Record<string, string> = {
  scans: "Website scans", syncs: "Integration syncs", feed_generation: "Feed generation", bulk_operations: "Bulk operations",
  webhook_deliveries: "Webhook deliveries", reports: "Reports",
};

export function checkDetail(name: string, c: HealthCheck): string {
  if (c.status === "not_used") return c.detail ?? "Not used in this configuration";
  if (c.status === "error") return c.error ?? "Not reachable";
  if (name === "worker") return `${c.workers} worker${c.workers === 1 ? "" : "s"} answered`;
  if (name === "scheduler") return `Last tick ${c.age_seconds}s ago`;
  return c.latency_ms !== undefined ? `${c.latency_ms} ms` : "OK";
}

export default function SystemStatusPage() {
  const q = useQuery({ queryKey: ["system-status"], queryFn: () => request<SystemStatus>("GET", "/system/status"), refetchInterval: 30_000 });
  const d = q.data;
  return (
    <>
      <PageHeader title="System status"
        description="Live checks of the services AgentReady depends on, and the background jobs of your store from the last 7 days. Nothing here is estimated."
        actions={<Button variant="outline" onClick={() => q.refetch()} disabled={q.isFetching}><RefreshCw className={`mr-1.5 h-4 w-4 ${q.isFetching ? "animate-spin" : ""}`} />Check now</Button>} />
      <ErrorNote error={q.error} />
      {q.isLoading || !d ? <Skeleton className="h-64" /> : (
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center gap-2">
                <CardTitle>Services</CardTitle>
                <Badge tone={OVERALL_TONE[d.health.status]} dot>{humanize(d.health.status)}</Badge>
              </div>
              <CardDescription>Checked {formatDate(d.health.checked_at)} · jobs run with {d.health.job_backend} · AI provider {d.health.ai_provider}{d.health.ai_configured ? "" : " (not configured)"}</CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="grid gap-2 sm:grid-cols-2">
                {Object.entries(d.health.checks).map(([name, c]) => (
                  <li key={name} className="flex items-start gap-2 rounded-md border p-3">
                    {c.status === "error" ? <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden /> : <CheckCircle2 className={`mt-0.5 h-4 w-4 shrink-0 ${c.status === "ok" ? "text-success" : "text-muted-foreground"}`} aria-hidden />}
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium">{CHECK_LABELS[name] ?? humanize(name)} <span className="sr-only">{c.status}</span></p>
                      <p className="break-words text-[12px] text-muted-foreground">{checkDetail(name, c)}</p>
                    </div>
                  </li>
                ))}
              </ul>
              {d.health.status !== "ok" && (
                <p className="mt-3 text-[12.5px] text-muted-foreground">
                  {d.health.status === "degraded" ? "The app works, but background work (scans, feeds, webhooks, scheduled jobs) is paused until the worker and scheduler are running again." : "The database or job queue is unreachable; changes cannot be saved right now."}
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Background jobs (last {d.period_days} days)</CardTitle><CardDescription>{d.active_jobs} queued or running right now.</CardDescription></CardHeader>
            <CardContent className="overflow-x-auto">
              <Table>
                <THead><TR><TH>Job type</TH><TH>By status</TH></TR></THead>
                <TBody>
                  {Object.entries(d.jobs).map(([kind, counts]) => (
                    <TR key={kind}>
                      <TD className="whitespace-nowrap">{JOB_LABELS[kind] ?? humanize(kind)}</TD>
                      <TD>
                        {Object.keys(counts).length === 0 ? <span className="text-muted-foreground">None</span> : (
                          <div className="flex flex-wrap gap-1.5">
                            {Object.entries(counts).map(([st, n]) => (
                              <Badge key={st} tone={st.includes("fail") || st === "completed_with_errors" ? "danger" : st === "completed" || st === "succeeded" ? "success" : "neutral"}>{humanize(st)} · {n}</Badge>
                            ))}
                          </div>
                        )}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Recent failures</CardTitle><CardDescription>Each failure shows the recorded reason and how to retry it safely.</CardDescription></CardHeader>
            <CardContent>
              {d.recent_failures.length === 0 ? <EmptyState icon={<Activity />} title="No failed jobs" description={`Nothing failed in the last ${d.period_days} days.`} /> : (
                <ul className="divide-y">
                  {d.recent_failures.map((f) => (
                    <li key={`${f.kind}-${f.id}`} className="py-3 text-[13px]">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone="danger">{humanize(f.kind)}</Badge>
                        {f.name && <span className="font-medium">{humanize(f.name)}</span>}
                        <span className="text-muted-foreground">{f.at ? formatDate(f.at) : ""}</span>
                      </div>
                      <p className="mt-1 break-words">{f.error}</p>
                      {f.detail && <p className="break-words text-[12px] text-muted-foreground">{f.detail}</p>}
                      <p className="mt-1 text-[12px] text-muted-foreground">Retry: <Link to={f.link} className="text-primary hover:underline">{f.retry}</Link></p>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </>
  );
}
