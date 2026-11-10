import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, CalendarClock, Minus, Pencil, Plus, RotateCcw, X } from "lucide-react";
import { toast } from "sonner";
import { ApprovalBadge, ErrorNote, QualityBadge } from "@/components/app/common";
import { LifecycleBadge, PriceStatusBadge, PublicationBadge, StockBadge, money } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/overlays";
import { commerce } from "@/services/commerce";
import type { CommerceProduct, Variant } from "@/types/commerce";
import { formatDate, humanize, timeAgo } from "@/utils/format";

type P = CommerceProduct & { evidence?: unknown[]; source_values?: Record<string, unknown> | null };

function useRefresh(id: string) {
  const qc = useQueryClient();
  return () => {
    for (const k of [["product", id], ["products"], ["overview"], ["pricing", id], ["price-history", id], ["inventory-tx", id], ["history", id], ["variants", id]]) {
      void qc.invalidateQueries({ queryKey: k });
    }
  };
}

async function run(fn: () => Promise<unknown>, ok: string, refresh: () => void, setErr?: (e: unknown) => void) {
  try {
    await fn();
    toast.success(ok);
    refresh();
    return true;
  } catch (e) {
    if (setErr) setErr(e);
    else toast.error((e as Error).message);
    return false;
  }
}

const dt = (local: string) => (local ? new Date(local).toISOString() : null);
const toLocal = (iso: string | null) => (iso ? new Date(new Date(iso).getTime() - new Date(iso).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "");

/* ================================================================ overview */
export function OverviewSection({ p }: { p: P }) {
  const refresh = useRefresh(p.id);
  const categories = useQuery({ queryKey: ["categories-managed"], queryFn: () => commerce.categories() });
  const rows: [string, React.ReactNode][] = [
    ["Lifecycle", <LifecycleBadge status={p.lifecycle_status} />],
    ["Publication", <span className="flex items-center gap-2"><PublicationBadge status={p.publication_status} />{p.is_public ? <span className="text-[12px] text-muted-foreground">visible in catalog API & feeds</span> : <span className="text-[12px] text-muted-foreground">not in public feeds</span>}</span>],
    ["Approval", <ApprovalBadge status={p.approval_status} />],
    ["Data quality", <span className="flex items-center gap-2"><QualityBadge status={p.quality_status} /><span className="tabular text-[12px] text-muted-foreground">{p.quality_score}% complete</span></span>],
    ["Price", <span className="flex items-center gap-2">{money(p.pricing.effective_price, p.pricing.currency)}<PriceStatusBadge status={p.pricing.price_status} /></span>],
    ["Stock", <StockBadge status={p.stock_status} available={p.inventory.available} />],
    ["Variants", p.variants.filter((v) => !v.archived_at).length || "None"],
    ["Source", humanize(p.origin)],
    ["Created", formatDate(p.created_at)],
    ["Updated", `${formatDate(p.updated_at)} (version ${p.version})`],
  ];
  return (
    <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
      <Card>
        <CardHeader><CardTitle>Status</CardTitle></CardHeader>
        <CardContent>
          <dl className="divide-y text-[13px]">
            {rows.map(([k, v]) => (
              <div key={k} className="grid grid-cols-[130px_1fr] items-center gap-3 py-2"><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></div>
            ))}
          </dl>
        </CardContent>
      </Card>
      <div className="space-y-4">
        <Card>
          <CardHeader><div><CardTitle>Category</CardTitle><CardDescription>{p.category ?? "No category"}</CardDescription></div></CardHeader>
          <CardContent>
            <NativeSelect className="w-full" aria-label="Assign category" value={p.category_id ?? ""}
              onChange={(e) => run(() => commerce.updateProduct(p.id, { category_id: e.target.value || null }), "Category updated", refresh)}>
              <option value="">{p.category && !p.category_id ? `Unmanaged: ${p.category}` : "No category"}</option>
              {categories.data?.items.map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
            </NativeSelect>
            <p className="mt-2 text-[12px] text-muted-foreground"><Link to="/app/categories" className="underline">Manage categories</Link></p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><CardTitle>Lifecycle</CardTitle><CardDescription>Separate from approval and publication.</CardDescription></div></CardHeader>
          <CardContent>
            <NativeSelect className="w-full" aria-label="Lifecycle status" value={p.lifecycle_status} disabled={!!p.deleted_at}
              onChange={(e) => run(() => commerce.updateProduct(p.id, { lifecycle_status: e.target.value }), "Lifecycle updated", refresh)}>
              <option value="draft">Draft</option><option value="active">Active</option>
              <option value="discontinued">Discontinued</option><option value="archived">Archived</option>
            </NativeSelect>
            {p.manual_overrides.length > 0 && (
              <p className="mt-3 text-[12px] text-muted-foreground">Managed in AgentReady (rescans raise conflicts instead of overwriting): {p.manual_overrides.join(", ")}</p>
            )}
            {!!p.open_conflicts && <p className="mt-2 text-[12.5px]"><Link to="/app/sync" className="font-medium underline">{p.open_conflicts} open sync conflict(s)</Link></p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

/* ================================================================ pricing */
export function PricingSection({ p }: { p: P }) {
  const refresh = useRefresh(p.id);
  const pricing = useQuery({ queryKey: ["pricing", p.id], queryFn: () => commerce.pricing(p.id) });
  const history = useQuery({ queryKey: ["price-history", p.id], queryFn: () => commerce.priceHistory(p.id) });
  const pr = pricing.data ?? p.pricing;
  const [f, setF] = useState<Record<string, string>>({});
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [sched, setSched] = useState({ price_type: "regular", new_amount: "", effective_at: "", reason: "" });
  const val = (k: string, d: string | null | undefined) => (k in f ? f[k] : d ?? "");

  const save = async () => {
    setBusy(true);
    setErr(null);
    const body: Record<string, unknown> = {};
    for (const k of ["regular_price", "sale_price", "cost_price", "currency", "tax_class"]) if (k in f) body[k] = f[k].trim() === "" ? null : f[k].trim();
    for (const k of ["sale_starts_at", "sale_ends_at"]) if (k in f) body[k] = dt(f[k]);
    if ("tax_included" in f) body.tax_included = f.tax_included === "" ? null : f.tax_included === "true";
    if (f.reason) body.reason = f.reason;
    const ok = await run(() => commerce.setPricing(p.id, body), "Pricing saved", refresh, setErr);
    if (ok) setF({});
    setBusy(false);
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
      <Card>
        <CardHeader><div><CardTitle>Prices</CardTitle><CardDescription>
            Current selling price: <b className="text-foreground">{money(pr.effective_price, pr.currency)}</b> {pr.price_status !== "regular" && <PriceStatusBadge status={pr.price_status} />}
          </CardDescription></div></CardHeader>
        <CardContent className="space-y-4">
          <ErrorNote error={err} />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Regular price" htmlFor="pr-reg"><Input id="pr-reg" inputMode="decimal" value={val("regular_price", pr.regular_price)} onChange={(e) => setF({ ...f, regular_price: e.target.value })} /></Field>
            <Field label="Currency" htmlFor="pr-cur" hint="Changing it does not convert amounts"><Input id="pr-cur" maxLength={3} className="uppercase" value={val("currency", pr.currency)} onChange={(e) => setF({ ...f, currency: e.target.value.toUpperCase() })} /></Field>
            <Field label="Sale price" htmlFor="pr-sale" hint="Leave empty for no sale"><Input id="pr-sale" inputMode="decimal" value={val("sale_price", pr.sale_price)} onChange={(e) => setF({ ...f, sale_price: e.target.value })} /></Field>
            <Field label="Cost price (private)" htmlFor="pr-cost" hint="Never shown in feeds or the API"><Input id="pr-cost" inputMode="decimal" value={val("cost_price", pr.cost_price)} onChange={(e) => setF({ ...f, cost_price: e.target.value })} /></Field>
            <Field label="Sale starts" htmlFor="pr-ss"><Input id="pr-ss" type="datetime-local" value={val("sale_starts_at", toLocal(pr.sale_starts_at))} onChange={(e) => setF({ ...f, sale_starts_at: e.target.value })} /></Field>
            <Field label="Sale ends" htmlFor="pr-se"><Input id="pr-se" type="datetime-local" value={val("sale_ends_at", toLocal(pr.sale_ends_at))} onChange={(e) => setF({ ...f, sale_ends_at: e.target.value })} /></Field>
            <Field label="Tax class" htmlFor="pr-tax"><Input id="pr-tax" value={val("tax_class", pr.tax_class)} onChange={(e) => setF({ ...f, tax_class: e.target.value })} /></Field>
            <Field label="Prices include tax" htmlFor="pr-ti">
              <NativeSelect id="pr-ti" className="w-full" value={val("tax_included", pr.tax_included === null ? "" : String(pr.tax_included))} onChange={(e) => setF({ ...f, tax_included: e.target.value })}>
                <option value="">Not configured</option><option value="true">Yes</option><option value="false">No</option>
              </NativeSelect>
            </Field>
            <Field label="Reason (recorded in history)" htmlFor="pr-reason" className="sm:col-span-2"><Input id="pr-reason" value={f.reason ?? ""} onChange={(e) => setF({ ...f, reason: e.target.value })} /></Field>
          </div>
          <div className="flex justify-end gap-2">
            {Object.keys(f).length > 0 && <Button variant="outline" onClick={() => { setF({}); setErr(null); }}>Discard</Button>}
            <Button onClick={save} loading={busy} disabled={!Object.keys(f).filter((k) => k !== "reason").length}>Save pricing</Button>
          </div>
        </CardContent>
      </Card>
      <div className="space-y-4">
        <Card>
          <CardHeader><div><CardTitle className="flex items-center gap-2"><CalendarClock className="h-4 w-4" /> Scheduled changes</CardTitle><CardDescription>Applied automatically at the set time. Current prices are unaffected until then.</CardDescription></div></CardHeader>
          <CardContent className="space-y-3">
            {(pr.scheduled_changes ?? []).map((c) => (
              <div key={c.id} className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-[13px]">
                <span>{humanize(c.price_type)} → <b>{c.new_amount ? money(c.new_amount, c.currency) : "end sale"}</b> on {formatDate(c.effective_at)}</span>
                <Button size="xs" variant="ghost" onClick={() => run(() => commerce.cancelSchedule(c.id), "Scheduled change cancelled", refresh)}><X /> Cancel</Button>
              </div>
            ))}
            {!pr.scheduled_changes?.length && <p className="text-[13px] text-muted-foreground">Nothing scheduled.</p>}
            <div className="grid grid-cols-2 gap-2">
              <NativeSelect aria-label="Price to schedule" value={sched.price_type} onChange={(e) => setSched({ ...sched, price_type: e.target.value })}>
                <option value="regular">Regular price</option><option value="sale">Sale price</option>
              </NativeSelect>
              <Input aria-label="New amount" placeholder={sched.price_type === "sale" ? "Amount (empty = end sale)" : "New amount"} inputMode="decimal" value={sched.new_amount} onChange={(e) => setSched({ ...sched, new_amount: e.target.value })} />
              <Input aria-label="Effective at" type="datetime-local" value={sched.effective_at} onChange={(e) => setSched({ ...sched, effective_at: e.target.value })} />
              <Input aria-label="Schedule reason" placeholder="Reason" value={sched.reason} onChange={(e) => setSched({ ...sched, reason: e.target.value })} />
            </div>
            <Button size="sm" variant="outline" disabled={!sched.effective_at}
              onClick={async () => {
                const ok = await run(() => commerce.schedule({ product_id: p.id, price_type: sched.price_type, new_amount: sched.new_amount || null,
                  effective_at: dt(sched.effective_at), reason: sched.reason || undefined }), "Price change scheduled", refresh);
                if (ok) setSched({ price_type: "regular", new_amount: "", effective_at: "", reason: "" });
              }}>
              Schedule change
            </Button>
          </CardContent>
        </Card>
      </div>
      <Card className="lg:col-span-2 overflow-hidden">
        <CardHeader><div><CardTitle>Price history</CardTitle><CardDescription>Every price change, newest first. Records cannot be edited or deleted.</CardDescription></div></CardHeader>
        {history.isLoading ? <div className="p-4"><Skeleton className="h-20" /></div> : !history.data?.items.length ? (
          <CardContent><p className="text-[13px] text-muted-foreground">No price changes recorded yet.</p></CardContent>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <THead><TR><TH>When</TH><TH>Price</TH><TH>Change</TH><TH>Type</TH><TH>By</TH><TH>Reason</TH></TR></THead>
              <TBody>
                {history.data.items.map((h) => (
                  <TR key={h.id}>
                    <TD className="whitespace-nowrap text-muted-foreground">{formatDate(h.created_at)}</TD>
                    <TD>{humanize(h.price_type)}{h.variant_id && <Badge className="ml-1">variant</Badge>}</TD>
                    <TD className="tabular whitespace-nowrap">{money(h.old_amount, h.currency)} → <b>{money(h.new_amount, h.currency)}</b></TD>
                    <TD><Badge>{humanize(h.change_type)}</Badge></TD>
                    <TD className="text-muted-foreground">{h.actor ?? humanize(h.actor_type)}</TD>
                    <TD className="max-w-[220px] truncate text-muted-foreground" title={h.reason ?? ""}>{h.reason ?? "—"}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ================================================================ inventory */
export function InventorySection({ p }: { p: P }) {
  const refresh = useRefresh(p.id);
  const tx = useQuery({ queryKey: ["inventory-tx", p.id], queryFn: () => commerce.transactions({ product_id: p.id, page_size: 50 }) });
  const inv = p.inventory;
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const hasTrackedVariants = p.variants.some((v) => !v.archived_at && v.track_inventory);
  const adjust = async (operation: string) => {
    setErr(null);
    const ok = await run(() => commerce.adjust({ product_id: p.id, operation, quantity: Number(qty || 0), reason: reason || undefined }),
      "Stock updated", refresh, setErr);
    if (ok) { setQty(""); setReason(""); }
  };
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
      <Card>
        <CardHeader><div><CardTitle>Stock</CardTitle><CardDescription>Available = on hand − reserved. {hasTrackedVariants && "This product also tracks stock per variant (see Variants)."}</CardDescription></div></CardHeader>
        <CardContent className="space-y-4">
          <ErrorNote error={err} />
          {inv.track_inventory ? (
            <>
              <div className="grid grid-cols-3 gap-3 text-center">
                {[["On hand", inv.on_hand], ["Reserved", inv.reserved], ["Available", inv.available]].map(([l, v]) => (
                  <div key={l as string} className="rounded-md border py-3"><p className="text-[12px] text-muted-foreground">{l}</p><p className="tabular text-xl font-semibold">{v}</p></div>
                ))}
              </div>
              <div className="flex items-center gap-2"><StockBadge status={p.stock_status} /><span className="text-[12px] text-muted-foreground">Low-stock threshold: {inv.effective_low_stock_threshold ?? "none"}</span></div>
              <div className="grid grid-cols-2 gap-2">
                <Input aria-label="Quantity" inputMode="numeric" placeholder="Quantity" value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ""))} />
                <Input aria-label="Reason" placeholder="Reason (e.g. stock take)" value={reason} onChange={(e) => setReason(e.target.value)} />
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={!qty} onClick={() => adjust("increase")}><Plus /> Add</Button>
                <Button size="sm" variant="outline" disabled={!qty} onClick={() => adjust("decrease")}><Minus /> Remove</Button>
                <Button size="sm" variant="outline" disabled={qty === ""} onClick={() => adjust("set")}>Set to</Button>
                <Button size="sm" variant="outline" disabled={!qty} onClick={() => adjust("reserve")}>Reserve</Button>
                <Button size="sm" variant="outline" disabled={!qty} onClick={() => adjust("release")}>Release</Button>
                <Button size="sm" variant="ghost" onClick={() => adjust("mark_out_of_stock")}>Mark out of stock</Button>
              </div>
            </>
          ) : (
            <p className="text-[13px] text-muted-foreground">Inventory is not tracked. Availability: <b className="text-foreground">{p.availability ?? "not stated"}</b>. Enable tracking to manage stock levels.</p>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Settings</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <label className="flex items-center gap-2 text-[13px]">
            <Checkbox checked={(settings.track_inventory as boolean | undefined) ?? inv.track_inventory}
              onCheckedChange={(v) => setSettings({ ...settings, track_inventory: !!v })} aria-label="Track inventory" /> Track inventory
          </label>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Low-stock threshold" htmlFor="inv-th" hint="Empty = catalog default"><Input id="inv-th" inputMode="numeric" defaultValue={inv.low_stock_threshold ?? ""}
              onChange={(e) => setSettings({ ...settings, low_stock_threshold: e.target.value === "" ? null : Number(e.target.value) })} /></Field>
            <Field label="Backorders" htmlFor="inv-bo">
              <NativeSelect id="inv-bo" className="w-full" defaultValue={inv.backorder_policy} onChange={(e) => setSettings({ ...settings, backorder_policy: e.target.value })}>
                <option value="deny">Not allowed (stock never negative)</option><option value="allow">Allowed</option>
              </NativeSelect>
            </Field>
            <Field label="Warehouse / location" htmlFor="inv-loc" className="sm:col-span-2"><Input id="inv-loc" defaultValue={inv.location ?? ""} onChange={(e) => setSettings({ ...settings, inventory_location: e.target.value || null })} /></Field>
            {!inv.track_inventory && !!settings.track_inventory && (
              <Field label="Starting stock" htmlFor="inv-start"><Input id="inv-start" inputMode="numeric" onChange={(e) => setSettings({ ...settings, stock_quantity: e.target.value === "" ? undefined : Number(e.target.value) })} /></Field>
            )}
          </div>
          <div className="flex justify-end">
            <Button size="sm" disabled={!Object.keys(settings).length}
              onClick={async () => { if (await run(() => commerce.setInventory(p.id, settings), "Inventory settings saved", refresh, setErr)) setSettings({}); }}>
              Save settings
            </Button>
          </div>
        </CardContent>
      </Card>
      <Card className="lg:col-span-2 overflow-hidden">
        <CardHeader><div><CardTitle>Inventory transactions</CardTitle><CardDescription>Every stock change, newest first.</CardDescription></div></CardHeader>
        {!tx.data?.items.length ? <CardContent><p className="text-[13px] text-muted-foreground">No stock changes yet.</p></CardContent> : (
          <div className="overflow-x-auto">
            <Table>
              <THead><TR><TH>When</TH><TH>Type</TH><TH>Field</TH><TH className="text-right">Before</TH><TH className="text-right">Change</TH><TH className="text-right">After</TH><TH>By</TH><TH>Reason</TH></TR></THead>
              <TBody>
                {tx.data.items.map((t) => (
                  <TR key={t.id}>
                    <TD className="whitespace-nowrap text-muted-foreground">{formatDate(t.created_at)}</TD>
                    <TD><Badge>{humanize(t.transaction_type)}</Badge>{t.variant_id && <Badge className="ml-1">variant</Badge>}</TD>
                    <TD className="text-muted-foreground">{humanize(t.quantity_field)}</TD>
                    <TD className="tabular text-right">{t.previous_quantity}</TD>
                    <TD className={`tabular text-right ${t.adjustment < 0 ? "text-destructive" : "text-success"}`}>{t.adjustment > 0 ? "+" : ""}{t.adjustment}</TD>
                    <TD className="tabular text-right font-medium">{t.new_quantity}</TD>
                    <TD className="text-muted-foreground">{t.actor ?? humanize(t.actor_type)}</TD>
                    <TD className="max-w-[200px] truncate text-muted-foreground">{t.reason ?? "—"}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ================================================================ variants */
function VariantDialog({ product, variant, open, onOpenChange, onSaved }: { product: P; variant: Variant | null; open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void }) {
  const [opts, setOpts] = useState<{ k: string; v: string }[]>([]);
  const [f, setF] = useState<Record<string, string>>({});
  const [track, setTrack] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const reset = () => {
    setOpts(variant ? Object.entries(variant.options).map(([k, v]) => ({ k, v })) : [{ k: "Size", v: "" }, { k: "Colour", v: "" }]);
    setF(variant ? { sku: variant.sku ?? "", regular_price: variant.regular_price ?? "", sale_price: variant.sale_price ?? "", image_url: variant.image_url ?? "" } : {});
    setTrack(variant?.track_inventory ?? false);
    setErr(null);
  };
  return (
    <Dialog open={open} onOpenChange={(o) => { if (o) reset(); onOpenChange(o); }}>
      <DialogContent onOpenAutoFocus={reset}>
        <DialogHeader>
          <DialogTitle>{variant ? `Edit variant ${variant.name ?? ""}` : "New variant"}</DialogTitle>
          <DialogDescription>Leave the price empty to use the product price ({money(product.pricing.regular_price, product.pricing.currency)}).</DialogDescription>
        </DialogHeader>
        <ErrorNote error={err} />
        <div className="space-y-2">
          <p className="text-[13px] font-medium">Options</p>
          {opts.map((o, i) => (
            <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2">
              <Input aria-label="Option name" placeholder="Option (e.g. Size)" value={o.k} onChange={(e) => setOpts(opts.map((x, j) => (j === i ? { ...x, k: e.target.value } : x)))} />
              <Input aria-label={`${o.k || "Option"} value`} placeholder="Value (e.g. M)" value={o.v} onChange={(e) => setOpts(opts.map((x, j) => (j === i ? { ...x, v: e.target.value } : x)))} />
              <Button variant="ghost" size="icon" aria-label="Remove option" onClick={() => setOpts(opts.filter((_, j) => j !== i))}><X /></Button>
            </div>
          ))}
          <Button size="xs" variant="outline" onClick={() => setOpts([...opts, { k: "", v: "" }])}><Plus /> Add option</Button>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="SKU" htmlFor="v-sku"><Input id="v-sku" value={f.sku ?? ""} onChange={(e) => setF({ ...f, sku: e.target.value })} /></Field>
          <Field label="Image URL" htmlFor="v-img"><Input id="v-img" value={f.image_url ?? ""} onChange={(e) => setF({ ...f, image_url: e.target.value })} /></Field>
          <Field label="Price" htmlFor="v-price"><Input id="v-price" inputMode="decimal" placeholder="Product price" value={f.regular_price ?? ""} onChange={(e) => setF({ ...f, regular_price: e.target.value })} /></Field>
          <Field label="Sale price" htmlFor="v-sale"><Input id="v-sale" inputMode="decimal" value={f.sale_price ?? ""} onChange={(e) => setF({ ...f, sale_price: e.target.value })} /></Field>
          {!variant && (
            <>
              <label className="flex items-center gap-2 text-[13px]"><Checkbox checked={track} onCheckedChange={(v) => setTrack(!!v)} aria-label="Track variant stock" /> Track stock</label>
              {track && <Field label="Starting stock" htmlFor="v-qty"><Input id="v-qty" inputMode="numeric" value={f.stock_quantity ?? ""} onChange={(e) => setF({ ...f, stock_quantity: e.target.value.replace(/\D/g, "") })} /></Field>}
            </>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button loading={busy} onClick={async () => {
            setBusy(true);
            setErr(null);
            const options = Object.fromEntries(opts.filter((o) => o.k.trim() && o.v.trim()).map((o) => [o.k.trim(), o.v.trim()]));
            const body: Record<string, unknown> = { options, sku: f.sku?.trim() || null, image_url: f.image_url?.trim() || null };
            if (variant) {
              if (!f.regular_price?.trim()) body.inherit_price = true;
              else { body.regular_price = f.regular_price.trim(); body.sale_price = f.sale_price?.trim() || null; }
            } else {
              if (f.regular_price?.trim()) body.regular_price = f.regular_price.trim();
              if (f.sale_price?.trim()) body.sale_price = f.sale_price.trim();
              body.track_inventory = track;
              if (track && f.stock_quantity) body.stock_quantity = Number(f.stock_quantity);
            }
            const ok = await run(() => (variant ? commerce.updateVariant(variant.id, body) : commerce.createVariant(product.id, body)),
              variant ? "Variant updated" : "Variant created", onSaved, setErr);
            setBusy(false);
            if (ok) onOpenChange(false);
          }}>{variant ? "Save variant" : "Create variant"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function VariantsSection({ p }: { p: P }) {
  const refresh = useRefresh(p.id);
  const [showArchived, setShowArchived] = useState(false);
  const variants = useQuery({ queryKey: ["variants", p.id, showArchived], queryFn: () => commerce.variants(p.id, showArchived) });
  const [edit, setEdit] = useState<Variant | null>(null);
  const [open, setOpen] = useState(false);
  const [stockFor, setStockFor] = useState<Variant | null>(null);
  const [qty, setQty] = useState("");
  const items = variants.data?.items ?? [];
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row items-center justify-between gap-2">
        <div>
          <CardTitle>Variants</CardTitle>
          <CardDescription>Each variant has its own SKU, price and stock. Not every product needs variants.</CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground"><Checkbox checked={showArchived} onCheckedChange={(v) => setShowArchived(!!v)} aria-label="Show archived" /> Archived</label>
          <Button size="sm" onClick={() => { setEdit(null); setOpen(true); }}><Plus /> Add variant</Button>
        </div>
      </CardHeader>
      {variants.isLoading ? <div className="p-4"><Skeleton className="h-16" /></div> : items.length === 0 ? (
        <CardContent><p className="text-[13px] text-muted-foreground">No variants. Add one for sizes, colours, capacities or other options.</p></CardContent>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <THead><TR><TH>Variant</TH><TH>SKU</TH><TH className="text-right">Price</TH><TH>Stock</TH><TH>Status</TH><TH className="w-40" /></TR></THead>
            <TBody>
              {items.map((v) => (
                <TR key={v.id} className={v.archived_at ? "opacity-60" : ""}>
                  <TD>
                    <p className="font-medium">{v.name ?? "Variant"}</p>
                    <p className="text-[12px] text-muted-foreground">{Object.entries(v.options).map(([k, x]) => `${k}: ${x}`).join(" · ")}</p>
                  </TD>
                  <TD className="font-mono text-[12px]">{v.sku ?? "—"}</TD>
                  <TD className="tabular text-right">{money(v.effective_price, v.currency)}{v.inherits_price && <p className="text-[11px] text-muted-foreground">product price</p>}</TD>
                  <TD><StockBadge status={v.stock_status} available={v.inventory?.available} /></TD>
                  <TD>{v.archived_at ? <Badge tone="warning">Archived</Badge> : v.is_enabled ? <Badge tone="success">Enabled</Badge> : <Badge>Disabled</Badge>}</TD>
                  <TD>
                    <div className="flex justify-end gap-1">
                      {v.archived_at ? (
                        <Button size="xs" variant="ghost" onClick={() => run(() => commerce.updateVariant(v.id, { archived: false }), "Variant restored", refresh)}><RotateCcw /> Restore</Button>
                      ) : (
                        <>
                          {v.track_inventory && <Button size="xs" variant="ghost" onClick={() => { setStockFor(v); setQty(String(v.inventory?.on_hand ?? 0)); }}>Stock</Button>}
                          <Button size="xs" variant="ghost" aria-label={`Edit ${v.name}`} onClick={() => { setEdit(v); setOpen(true); }}><Pencil /></Button>
                          <Button size="xs" variant="ghost" onClick={() => run(() => commerce.updateVariant(v.id, { is_enabled: !v.is_enabled }), v.is_enabled ? "Variant disabled" : "Variant enabled", refresh)}>{v.is_enabled ? "Disable" : "Enable"}</Button>
                          <Button size="xs" variant="ghost" aria-label={`Archive ${v.name}`} onClick={() => run(() => commerce.archiveVariant(v.id), "Variant archived (history kept)", refresh)}><Archive /></Button>
                        </>
                      )}
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </div>
      )}
      <VariantDialog product={p} variant={edit} open={open} onOpenChange={setOpen} onSaved={refresh} />
      <Dialog open={!!stockFor} onOpenChange={(o) => !o && setStockFor(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Set stock · {stockFor?.name}</DialogTitle><DialogDescription>Recorded as an inventory transaction.</DialogDescription></DialogHeader>
          <Field label="Stock on hand" htmlFor="vs-qty"><Input id="vs-qty" inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ""))} /></Field>
          <DialogFooter>
            <Button variant="outline" onClick={() => setStockFor(null)}>Cancel</Button>
            <Button onClick={async () => { if (await run(() => commerce.adjust({ product_id: p.id, variant_id: stockFor!.id, operation: "set", quantity: Number(qty || 0) }), "Variant stock updated", refresh)) setStockFor(null); }}>Save</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/* ================================================================ images */
export function ImagesSection({ p }: { p: P }) {
  const refresh = useRefresh(p.id);
  const [url, setUrl] = useState("");
  const [alt, setAlt] = useState("");
  const save = (images: { url: string; alt?: string | null }[], msg: string) => run(() => commerce.updateProduct(p.id, { images }), msg, refresh);
  return (
    <Card>
      <CardHeader><div><CardTitle>Images & media</CardTitle><CardDescription>The first image is the main image. Editing images marks them as merchant-managed.</CardDescription></div></CardHeader>
      <CardContent className="space-y-4">
        {p.images.length === 0 && <p className="text-[13px] text-muted-foreground">No images.</p>}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {p.images.map((img, i) => (
            <div key={img.url} className="group relative rounded-md border p-1.5">
              <img src={img.url} alt={img.alt ?? ""} referrerPolicy="no-referrer" className="aspect-square w-full rounded object-cover" />
              <p className="mt-1 truncate text-[11px] text-muted-foreground" title={img.url}>{i === 0 ? "Main · " : ""}{img.alt || "no alt text"}</p>
              <div className="mt-1 flex gap-1">
                {i > 0 && <Button size="xs" variant="outline" onClick={() => save([img, ...p.images.filter((_, j) => j !== i)], "Main image updated")}>Make main</Button>}
                <Button size="xs" variant="ghost" aria-label="Remove image" onClick={() => save(p.images.filter((_, j) => j !== i), "Image removed")}><X /></Button>
              </div>
            </div>
          ))}
        </div>
        <div className="grid gap-2 sm:grid-cols-[1fr_200px_auto]">
          <Input aria-label="Image URL" placeholder="https://…/image.jpg" value={url} onChange={(e) => setUrl(e.target.value)} />
          <Input aria-label="Alt text" placeholder="Alt text" value={alt} onChange={(e) => setAlt(e.target.value)} />
          <Button variant="outline" disabled={!/^https?:\/\/\S+$/.test(url)} onClick={async () => { if (await save([...p.images, { url, alt: alt || null }], "Image added")) { setUrl(""); setAlt(""); } }}><Plus /> Add image</Button>
        </div>
      </CardContent>
    </Card>
  );
}

/* ================================================================ SEO & structured data */
function jsonLd(p: P) {
  const node: Record<string, unknown> = { "@context": "https://schema.org", "@type": "Product", name: p.name };
  if (p.description) node.description = p.description;
  if (p.sku) node.sku = p.sku;
  if (p.gtin) node[`gtin${p.gtin.length}`] = p.gtin;
  if (p.brand) node.brand = { "@type": "Brand", name: p.brand };
  if (p.category) node.category = p.category;
  if (p.images.length) node.image = p.images.map((i) => i.url);
  if (p.pricing.effective_price && p.pricing.currency) {
    node.offers = { "@type": "Offer", price: p.pricing.effective_price, priceCurrency: p.pricing.currency,
      ...(p.availability ? { availability: `https://schema.org/${p.availability}` } : {}) };
  }
  return node;
}

export function SeoSection({ p }: { p: P }) {
  const refresh = useRefresh(p.id);
  const [title, setTitle] = useState(p.seo_title ?? "");
  const [desc, setDesc] = useState(p.seo_description ?? "");
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>Search appearance</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <Field label="SEO title" htmlFor="seo-t" hint={`${title.length}/200`}><Input id="seo-t" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} placeholder={p.name ?? ""} /></Field>
          <Field label="Meta description" htmlFor="seo-d" hint={`${desc.length}/500`}><Textarea id="seo-d" rows={4} maxLength={500} value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
          <div className="rounded-md border p-3">
            <p className="truncate text-[15px] text-info">{title || p.name}</p>
            <p className="truncate text-[12px] text-success">{p.product_url ?? p.source_url ?? "—"}</p>
            <p className="line-clamp-2 text-[12.5px] text-muted-foreground">{desc || p.description || "No description"}</p>
          </div>
          <div className="flex justify-end">
            <Button size="sm" disabled={title === (p.seo_title ?? "") && desc === (p.seo_description ?? "")}
              onClick={() => run(() => commerce.updateProduct(p.id, { seo_title: title || null, seo_description: desc || null }), "SEO saved", refresh)}>Save</Button>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><div><CardTitle>Structured data (schema.org JSON-LD)</CardTitle><CardDescription>Preview of what JSON-LD exports contain for this product. Only published products appear in public feeds.</CardDescription></div></CardHeader>
        <CardContent><pre className="max-h-[420px] overflow-auto rounded-md border bg-muted/30 p-3 font-mono text-[11.5px]">{JSON.stringify(jsonLd(p), null, 2)}</pre></CardContent>
      </Card>
    </div>
  );
}

/* ================================================================ change history */
export function HistorySection({ p }: { p: P }) {
  const h = useQuery({ queryKey: ["history", p.id], queryFn: () => commerce.history(p.id) });
  if (h.isLoading) return <Skeleton className="h-40" />;
  if (h.error) return <ErrorNote error={h.error} />;
  const items = h.data?.items ?? [];
  if (!items.length) return <p className="text-[13px] text-muted-foreground">No recorded changes yet.</p>;
  return (
    <Card>
      <CardContent className="pt-5">
        <ol className="relative space-y-4 border-l pl-5">
          {items.map((e, i) => (
            <li key={i} className="relative">
              <span className={`absolute -left-[25px] top-1 h-2.5 w-2.5 rounded-full ${e.kind === "price" ? "bg-info" : e.kind === "inventory" ? "bg-warning" : "bg-muted-foreground"}`} />
              <p className="text-[13px]">{e.summary}</p>
              <p className="text-[11.5px] text-muted-foreground">
                {timeAgo(e.at)} · {formatDate(e.at)} · {e.actor ?? humanize(e.actor_type)} · <span className="font-mono">{e.action}</span>
                {e.change_type ? ` · ${humanize(e.change_type)}` : ""}
              </p>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

