import { useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { ErrorNote } from "@/components/app/common";
import { Badge, Button, Field, Input, NativeSelect, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/overlays";
import { commerce } from "@/services/commerce";
import type { BulkOperation, BulkPreview, Category, Lifecycle, PriceStatus, Publication, StockStatus } from "@/types/commerce";
import { humanize } from "@/utils/format";

/* ------------------------------------------------------------------ formatting */
export function money(amount: string | number | null | undefined, currency: string | null | undefined): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!currency) return n.toFixed(2);
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency}`;
  }
}

export function display(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.length ? v.map(display).join(", ") : "—";
  return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${humanize(k)}: ${display(x)}`).join("; ");
}

/* ------------------------------------------------------------------ badges */
const STOCK: Record<StockStatus, ["success" | "warning" | "danger" | "info" | "neutral", string]> = {
  in_stock: ["success", "In stock"],
  low_stock: ["warning", "Low stock"],
  out_of_stock: ["danger", "Out of stock"],
  backorder: ["info", "Backorder"],
  untracked: ["neutral", "Not tracked"],
};
export function StockBadge({ status, available }: { status: StockStatus; available?: number }) {
  const [tone, label] = STOCK[status] ?? ["neutral", humanize(status)];
  return <Badge tone={tone} dot>{label}{available !== undefined && status !== "untracked" ? ` · ${available}` : ""}</Badge>;
}

export function LifecycleBadge({ status }: { status: Lifecycle }) {
  const tone = status === "active" ? "success" : status === "draft" ? "neutral" : status === "archived" ? "warning" : "info";
  return <Badge tone={tone}>{humanize(status)}</Badge>;
}

export function PublicationBadge({ status }: { status: Publication }) {
  return status === "published" ? <Badge tone="solid">Published</Badge> : <Badge>Unpublished</Badge>;
}

export function PriceStatusBadge({ status }: { status: PriceStatus }) {
  if (status === "on_sale") return <Badge tone="info">On sale</Badge>;
  if (status === "sale_scheduled") return <Badge>Sale scheduled</Badge>;
  if (status === "no_price") return <Badge tone="warning">No price</Badge>;
  return null;
}

/* ------------------------------------------------------------------ confirm */
export function ConfirmDialog({ open, onOpenChange, title, description, confirmLabel = "Confirm", destructive, onConfirm }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; description?: ReactNode; confirmLabel?: string;
  destructive?: boolean; onConfirm: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            variant={destructive ? "destructive" : "default"}
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm();
                onOpenChange(false);
              } catch (e) {
                toast.error((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ bulk operations */
export const BULK_OPS: { key: string; label: string; group: string }[] = [
  { key: "approve", label: "Approve", group: "Review" },
  { key: "send_to_review", label: "Send back to review", group: "Review" },
  { key: "reject", label: "Reject", group: "Review" },
  { key: "publish", label: "Publish", group: "Publication" },
  { key: "unpublish", label: "Unpublish", group: "Publication" },
  { key: "activate", label: "Set active", group: "Lifecycle" },
  { key: "discontinue", label: "Mark discontinued", group: "Lifecycle" },
  { key: "archive", label: "Archive", group: "Lifecycle" },
  { key: "restore", label: "Restore", group: "Lifecycle" },
  { key: "delete", label: "Delete (restorable)", group: "Lifecycle" },
  { key: "assign_category", label: "Assign category", group: "Details" },
  { key: "set_brand", label: "Update brand", group: "Details" },
  { key: "set_product_type", label: "Set product type", group: "Details" },
  { key: "add_attribute", label: "Add / set attribute", group: "Details" },
  { key: "remove_attribute", label: "Remove attribute", group: "Details" },
  { key: "price_adjust", label: "Adjust prices", group: "Pricing" },
  { key: "set_sale", label: "Set sale price", group: "Pricing" },
  { key: "clear_sale", label: "End sale", group: "Pricing" },
  { key: "schedule_price", label: "Schedule price change", group: "Pricing" },
  { key: "inventory_adjust", label: "Adjust stock", group: "Inventory" },
  { key: "inventory_settings", label: "Inventory settings", group: "Inventory" },
];

function ParamsForm({ op, params, set, categories }: { op: string; params: Record<string, unknown>; set: (k: string, v: unknown) => void; categories: Category[] }) {
  const str = (k: string) => (params[k] as string | undefined) ?? "";
  const priceMode = (
    <Field label="Change" htmlFor="b-mode">
      <NativeSelect id="b-mode" className="w-full" value={str("mode")} onChange={(e) => set("mode", e.target.value)}>
        <option value="percent_increase">Increase by %</option>
        <option value="percent_decrease">Decrease by %</option>
        <option value="fixed_increase">Increase by amount</option>
        <option value="fixed_decrease">Decrease by amount</option>
        <option value="set">Set to amount</option>
      </NativeSelect>
    </Field>
  );
  const fixed = !str("mode").startsWith("percent");
  switch (op) {
    case "assign_category":
      return (
        <Field label="Category" htmlFor="b-cat">
          <NativeSelect id="b-cat" className="w-full" value={str("category_id")} onChange={(e) => set("category_id", e.target.value || null)}>
            <option value="">No category (remove)</option>
            {categories.map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
          </NativeSelect>
        </Field>
      );
    case "set_brand":
      return <Field label="Brand" htmlFor="b-brand"><Input id="b-brand" value={str("brand")} onChange={(e) => set("brand", e.target.value)} /></Field>;
    case "set_product_type":
      return <Field label="Product type" htmlFor="b-type" hint="e.g. physical, digital, service"><Input id="b-type" value={str("product_type")} onChange={(e) => set("product_type", e.target.value)} /></Field>;
    case "add_attribute":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Attribute" htmlFor="b-al"><Input id="b-al" value={str("label")} onChange={(e) => set("label", e.target.value)} /></Field>
          <Field label="Value" htmlFor="b-av"><Input id="b-av" value={str("value")} onChange={(e) => set("value", e.target.value)} /></Field>
          <Field label="Unit" htmlFor="b-au"><Input id="b-au" value={str("unit")} onChange={(e) => set("unit", e.target.value || undefined)} /></Field>
        </div>
      );
    case "remove_attribute":
      return <Field label="Attribute key" htmlFor="b-rk" hint="e.g. warranty"><Input id="b-rk" value={str("key")} onChange={(e) => set("key", e.target.value)} /></Field>;
    case "price_adjust":
    case "schedule_price":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Price" htmlFor="b-target">
            <NativeSelect id="b-target" className="w-full" value={str("target") || "regular"} onChange={(e) => set("target", e.target.value)}>
              <option value="regular">Regular price</option>
              <option value="sale">Sale price</option>
            </NativeSelect>
          </Field>
          {priceMode}
          <Field label={fixed ? "Amount" : "Percent"} htmlFor="b-val"><Input id="b-val" inputMode="decimal" value={str("value")} onChange={(e) => set("value", e.target.value)} /></Field>
          {fixed && <Field label="Currency" htmlFor="b-cur" hint="Products in other currencies are not changed"><Input id="b-cur" maxLength={3} className="uppercase" value={str("currency")} onChange={(e) => set("currency", e.target.value.toUpperCase())} /></Field>}
          {op === "schedule_price" && (
            <Field label="Effective at" htmlFor="b-when" className="sm:col-span-2">
              <Input id="b-when" type="datetime-local" onChange={(e) => set("effective_at", e.target.value ? new Date(e.target.value).toISOString() : "")} />
            </Field>
          )}
          <Field label="Reason (optional)" htmlFor="b-reason" className="sm:col-span-3"><Input id="b-reason" value={str("reason")} onChange={(e) => set("reason", e.target.value)} /></Field>
        </div>
      );
    case "set_sale":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Sale" htmlFor="b-smode">
            <NativeSelect id="b-smode" className="w-full" value={str("mode")} onChange={(e) => set("mode", e.target.value)}>
              <option value="percent_off">% off regular price</option>
              <option value="fixed_price">Fixed sale price</option>
            </NativeSelect>
          </Field>
          <Field label={str("mode") === "fixed_price" ? "Sale price" : "Percent off"} htmlFor="b-sval"><Input id="b-sval" inputMode="decimal" value={str("value")} onChange={(e) => set("value", e.target.value)} /></Field>
          {str("mode") === "fixed_price" && <Field label="Currency" htmlFor="b-scur"><Input id="b-scur" maxLength={3} value={str("currency")} onChange={(e) => set("currency", e.target.value.toUpperCase())} /></Field>}
          <Field label="Starts (optional)" htmlFor="b-ss"><Input id="b-ss" type="datetime-local" onChange={(e) => set("starts_at", e.target.value ? new Date(e.target.value).toISOString() : undefined)} /></Field>
          <Field label="Ends (optional)" htmlFor="b-se"><Input id="b-se" type="datetime-local" onChange={(e) => set("ends_at", e.target.value ? new Date(e.target.value).toISOString() : undefined)} /></Field>
        </div>
      );
    case "inventory_adjust":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Change" htmlFor="b-iop">
            <NativeSelect id="b-iop" className="w-full" value={str("operation")} onChange={(e) => set("operation", e.target.value)}>
              <option value="increase">Increase stock</option>
              <option value="decrease">Decrease stock</option>
              <option value="set">Set stock to</option>
              <option value="mark_out_of_stock">Mark out of stock</option>
            </NativeSelect>
          </Field>
          {str("operation") !== "mark_out_of_stock" && (
            <Field label="Quantity" htmlFor="b-iq"><Input id="b-iq" inputMode="numeric" value={String(params.quantity ?? "")} onChange={(e) => set("quantity", e.target.value === "" ? "" : Number(e.target.value))} /></Field>
          )}
          <Field label="Reason" htmlFor="b-ir"><Input id="b-ir" value={str("reason")} onChange={(e) => set("reason", e.target.value)} /></Field>
        </div>
      );
    case "inventory_settings":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Tracking" htmlFor="b-tr">
            <NativeSelect id="b-tr" className="w-full" value={params.track_inventory === undefined ? "" : String(params.track_inventory)}
              onChange={(e) => set("track_inventory", e.target.value === "" ? undefined : e.target.value === "true")}>
              <option value="">Unchanged</option><option value="true">Track inventory</option><option value="false">Don't track</option>
            </NativeSelect>
          </Field>
          <Field label="Low-stock threshold" htmlFor="b-th"><Input id="b-th" inputMode="numeric" value={String(params.low_stock_threshold ?? "")} onChange={(e) => set("low_stock_threshold", e.target.value === "" ? undefined : Number(e.target.value))} /></Field>
          <Field label="Backorders" htmlFor="b-bo">
            <NativeSelect id="b-bo" className="w-full" value={str("backorder_policy")} onChange={(e) => set("backorder_policy", e.target.value || undefined)}>
              <option value="">Unchanged</option><option value="deny">Not allowed</option><option value="allow">Allowed</option>
            </NativeSelect>
          </Field>
        </div>
      );
    default:
      return <p className="text-[13px] text-muted-foreground">No options needed.</p>;
  }
}

const DEFAULT_PARAMS: Record<string, Record<string, unknown>> = {
  price_adjust: { mode: "percent_increase", target: "regular" },
  schedule_price: { mode: "percent_increase", target: "regular" },
  set_sale: { mode: "percent_off" },
  inventory_adjust: { operation: "increase" },
};

function cleanParams(p: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined && v !== ""));
}

export function BulkDialog({ open, onOpenChange, productIds, initialOperation, onDone, categories = [] }: {
  open: boolean; onOpenChange: (o: boolean) => void; productIds: string[]; initialOperation?: string; onDone?: () => void;
  categories?: Category[];
}) {
  const qc = useQueryClient();
  const [op, setOp] = useState(initialOperation ?? "approve");
  const [params, setParams] = useState<Record<string, unknown>>(DEFAULT_PARAMS[initialOperation ?? ""] ?? {});
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  const [result, setResult] = useState<BulkOperation | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setOp(initialOperation ?? "approve");
      setParams(DEFAULT_PARAMS[initialOperation ?? ""] ?? {});
      setPreview(null);
      setResult(null);
      setError(null);
    }
  }, [open, initialOperation]);

  // poll background operations for real progress
  useEffect(() => {
    if (!result || !["queued", "running"].includes(result.status)) return;
    const t = setInterval(async () => {
      try {
        setResult(await commerce.bulkOperation(result.id));
      } catch { /* keep polling */ }
    }, 1500);
    return () => clearInterval(t);
  }, [result]);

  const finished = result && !["queued", "running"].includes(result.status);
  useEffect(() => {
    if (finished) {
      void qc.invalidateQueries();
      onDone?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished]);

  const doPreview = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await commerce.bulk({ operation: op, product_ids: productIds, params: cleanParams(params), dry_run: true });
      setPreview(r.preview ?? null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const execute = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await commerce.bulk({ operation: op, product_ids: productIds, params: preview?.params ?? cleanParams(params), dry_run: false });
      setResult(r.operation ?? null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const keys = preview ? Array.from(new Set(preview.items.flatMap((i) => Object.keys(i.after ?? {})))) : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Bulk update · {productIds.length} product{productIds.length === 1 ? "" : "s"} selected</DialogTitle>
          <DialogDescription>Preview every change before it is applied. Each product is validated again when the operation runs.</DialogDescription>
        </DialogHeader>
        <ErrorNote error={error} />

        {!preview && !result && (
          <div className="space-y-4">
            <Field label="Operation" htmlFor="b-op">
              <NativeSelect id="b-op" className="w-full" value={op} onChange={(e) => { setOp(e.target.value); setParams(DEFAULT_PARAMS[e.target.value] ?? {}); }}>
                {Array.from(new Set(BULK_OPS.map((o) => o.group))).map((g) => (
                  <optgroup key={g} label={g}>
                    {BULK_OPS.filter((o) => o.group === g).map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
                  </optgroup>
                ))}
              </NativeSelect>
            </Field>
            <ParamsForm op={op} params={params} set={(k, v) => setParams((p) => ({ ...p, [k]: v }))} categories={categories} />
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button onClick={doPreview} loading={busy}>Preview changes</Button>
            </DialogFooter>
          </div>
        )}

        {preview && !result && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2 text-[13px]">
              <Badge>{preview.label}</Badge>
              <Badge tone="success">{preview.valid} will change</Badge>
              {preview.invalid > 0 && <Badge tone="danger">{preview.invalid} cannot be changed</Badge>}
              {preview.run_mode === "background" && <Badge tone="info">Runs in the background</Badge>}
            </div>
            <div className="max-h-[45vh] overflow-auto rounded-md border">
              <Table>
                <THead><TR><TH>Product</TH>{keys.map((k) => <TH key={k}>{humanize(k)}</TH>)}<TH>Status</TH></TR></THead>
                <TBody>
                  {preview.items.map((i) => (
                    <TR key={i.product_id}>
                      <TD className="max-w-[220px] truncate font-medium">{i.name ?? i.product_id}</TD>
                      {keys.map((k) => {
                        const before = display(i.before?.[k]);
                        const after = display(i.after?.[k]);
                        return <TD key={k} className="whitespace-nowrap text-[12.5px]">{i.status === "ok" ? (before === after ? after : <><span className="text-muted-foreground line-through">{before}</span> → <b>{after}</b></>) : "—"}</TD>;
                      })}
                      <TD>{i.status === "ok" ? <Badge tone="success">OK</Badge> : <span className="text-[12px] text-destructive">{i.error}</span>}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setPreview(null)}>Back</Button>
              <Button onClick={execute} loading={busy} disabled={preview.valid === 0}>
                Confirm: apply to {preview.valid} product{preview.valid === 1 ? "" : "s"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {result && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              {!finished ? <Loader2 className="h-4 w-4 animate-spin" /> : result.status === "completed" ? <CheckCircle2 className="h-4 w-4 text-success" /> :
                result.status === "failed" ? <XCircle className="h-4 w-4 text-destructive" /> : <AlertTriangle className="h-4 w-4 text-warning" />}
              {!finished ? `Processing ${result.processed} of ${result.total}…` :
                result.status === "completed" ? `All ${result.succeeded} product(s) updated` :
                `${result.succeeded} succeeded, ${result.failed} failed`}
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full bg-primary transition-all" style={{ width: `${result.total ? (result.processed / result.total) * 100 : 0}%` }} />
            </div>
            {finished && result.items && result.items.some((i) => i.status === "failed") && (
              <div className="max-h-[40vh] overflow-auto rounded-md border">
                <Table>
                  <THead><TR><TH>Failed product</TH><TH>Reason</TH></TR></THead>
                  <TBody>
                    {result.items.filter((i) => i.status === "failed").map((i) => (
                      <TR key={i.id}><TD className="font-medium">{i.name ?? i.product_id}</TD><TD className="text-[12.5px] text-destructive">{i.error}</TD></TR>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
            <DialogFooter><Button onClick={() => onOpenChange(false)} disabled={!finished}>Close</Button></DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
