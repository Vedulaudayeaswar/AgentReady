import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, ChevronLeft, ChevronRight, Moon, Sun } from "lucide-react";
import { Badge, Button } from "@/components/ui/primitives";
import { useTheme } from "@/hooks/useTheme";
import { cn } from "@/utils/cn";
import { humanize } from "@/utils/format";
import type { ApprovalStatus, QualityStatus, ScanStatus, Severity } from "@/types/api";

export function Logo({ className, to = "/" }: { className?: string; to?: string }) {
  return (
    <Link to={to} className={cn("flex items-center gap-2 font-semibold tracking-tight", className)}>
      <span className="grid h-6 w-6 place-items-center rounded-md bg-primary text-[11px] font-bold text-primary-foreground">A</span>
      <span>AgentReady</span>
    </Link>
  );
}

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <Button variant="ghost" size="icon" onClick={toggle} aria-label="Toggle theme">
      {theme === "dark" ? <Sun /> : <Moon />}
    </Button>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {description && <p className="mt-1 text-[13px] text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function EmptyState({ icon, title, description, action }: { icon: ReactNode; title: string; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed px-6 py-14 text-center">
      <div className="mb-3 grid h-10 w-10 place-items-center rounded-full bg-muted text-muted-foreground [&_svg]:size-5">{icon}</div>
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="mt-1 max-w-sm text-[13px] text-muted-foreground">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{msg}</span>
    </div>
  );
}

export function Pagination({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <div className="flex items-center justify-between px-3 py-2.5 text-[12.5px] text-muted-foreground">
      <span className="tabular">
        {from}–{to} of {total}
      </span>
      <div className="flex items-center gap-1">
        <Button variant="outline" size="icon" className="h-7 w-7" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Previous page">
          <ChevronLeft />
        </Button>
        <span className="tabular px-2">
          {page} / {pages}
        </span>
        <Button variant="outline" size="icon" className="h-7 w-7" disabled={page >= pages} onClick={() => onPage(page + 1)} aria-label="Next page">
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ status badges */
const QUALITY: Record<QualityStatus, { tone: "success" | "warning" | "danger" | "info" | "neutral"; label: string }> = {
  valid: { tone: "success", label: "Valid" },
  needs_review: { tone: "warning", label: "Needs review" },
  incomplete: { tone: "neutral", label: "Incomplete" },
  duplicate_candidate: { tone: "info", label: "Possible duplicate" },
  invalid: { tone: "danger", label: "Invalid" },
};
export function QualityBadge({ status }: { status: QualityStatus }) {
  const q = QUALITY[status] ?? { tone: "neutral", label: humanize(status) };
  return (
    <Badge tone={q.tone} dot>
      {q.label}
    </Badge>
  );
}

export function ApprovalBadge({ status }: { status: ApprovalStatus }) {
  const tone = status === "approved" ? "success" : status === "rejected" ? "danger" : "neutral";
  return <Badge tone={tone}>{status === "needs_review" ? "Pending" : humanize(status)}</Badge>;
}

export function ScanStatusBadge({ status }: { status: ScanStatus | "completed_with_warnings" }) {
  const map: Record<ScanStatus | "completed_with_warnings", ["success" | "warning" | "danger" | "info" | "neutral", string]> = {
    completed_with_warnings: ["warning", "Completed with warnings"],
    queued: ["neutral", "Queued"],
    running: ["info", "Running"],
    awaiting_review: ["warning", "Review changes"],
    completed: ["success", "Completed"],
    failed: ["danger", "Failed"],
    cancelled: ["neutral", "Cancelled"],
  };
  const [tone, label] = map[status] ?? ["neutral", status];
  return (
    <Badge tone={tone} dot>
      {label}
    </Badge>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const tone = severity === "error" ? "danger" : severity === "warning" ? "warning" : "neutral";
  return <Badge tone={tone}>{humanize(severity)}</Badge>;
}

export function MethodBadge({ method, model }: { method: string; model?: string | null }) {
  if (method === "deterministic") return <Badge tone="neutral">Deterministic</Badge>;
  if (method === "merchant") return <Badge tone="solid">Merchant edit</Badge>;
  if (method === "ai_mock") return <Badge tone="warning">Mock AI (not a real model)</Badge>;
  if (method === "integration") return <Badge tone="neutral">Store API</Badge>;
  return <Badge tone="info">AI · {model ?? "model"}</Badge>;
}

export function Stat({ label, value, hint, className }: { label: string; value: ReactNode; hint?: ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-lg border bg-card px-4 py-3.5", className)}>
      <p className="text-[12px] font-medium text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular tracking-tight">{value}</p>
      {hint && <p className="mt-0.5 text-[12px] text-muted-foreground">{hint}</p>}
    </div>
  );
}
