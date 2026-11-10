import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Boxes, CalendarClock, Layers, Plus, Search, Tag, X } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader, Pagination, Stat } from "@/components/app/common";
import { BulkDialog, PriceStatusBadge, StockBadge, money } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { commerce } from "@/services/commerce";
import { formatDate, humanize, timeAgo } from "@/utils/format";

/* ================================================================ Inventory */
export function InventoryPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const low = useQuery({ queryKey: ["low-stock"], queryFn: commerce.lowStock });
  const [txPage, setTxPage] = useState(1);
  const tx = useQuery({ queryKey: ["inventory-tx-all", txPage], queryFn: () => commerce.transactions({ page: txPage, page_size: 25 }), placeholderData: keepPreviousData });
  const [qtys, setQtys] = useState<Record<string, string>>({});
  const key = (i: { product_id: string; variant_id: string | null }) => `${i.product_id}:${i.variant_id ?? ""}`;
  const adjust = async (i: { product_id: string; variant_id: string | null; name: string }, operation: string) => {
    const q = Number(qtys[key(i)] || 0);
    try {
      await commerce.adjust({ product_id: i.product_id, variant_id: i.variant_id, operation, quantity: q, reason: "Restock from Inventory page" });
      toast.success(`Stock updated for ${i.name}`);
      setQtys({ ...qtys, [key(i)]: "" });
      void qc.invalidateQueries({ queryKey: ["low-stock"] });
      void qc.invalidateQueries({ queryKey: ["inventory-tx-all"] });
      void qc.invalidateQueries({ queryKey: ["overview"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  };
  const c = low.data?.counts;
  return (
    <>
      <PageHeader title="Inventory" description="Stock levels for products and variants with inventory tracking. Every change is recorded as a transaction."
        actions={<Button size="sm" variant="outline" onClick={() => navigate("/app/catalog?stock_status=untracked")}>Enable tracking on products</Button>} />
      <ErrorNote error={low.error} />
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-5">
        {(["in_stock", "low_stock", "out_of_stock", "backorder", "untracked"] as const).map((s) => (
          <Link key={s} to={`/app/catalog?stock_status=${s}`}><Stat label={humanize(s.replace("untracked", "not tracked"))} value={c ? c[s] : "—"} className="hover:bg-accent/40" /></Link>
        ))}
      </div>
      <Card className="mb-6 overflow-hidden">
        <CardHeader><div><CardTitle>Needs attention</CardTitle><CardDescription>At or below the low-stock threshold, out of stock, or on backorder.</CardDescription></div></CardHeader>
        {low.isLoading ? <div className="p-4"><Skeleton className="h-24" /></div> : !low.data?.items.length ? (
          <CardContent><EmptyState icon={<Boxes />} title="Nothing low on stock" description="Products with inventory tracking that run low will appear here." /></CardContent>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <THead><TR><TH>Item</TH><TH>SKU</TH><TH>Status</TH><TH className="text-right">On hand</TH><TH className="text-right">Available</TH><TH className="text-right">Threshold</TH><TH className="w-72">Restock</TH></TR></THead>
              <TBody>
                {low.data.items.map((i) => (
                  <TR key={key(i)}>
                    <TD className="max-w-[260px] truncate font-medium"><Link to={`/app/catalog/${i.product_id}`} className="hover:underline">{i.name}</Link></TD>
                    <TD className="font-mono text-[12px]">{i.sku ?? "—"}</TD>
                    <TD><StockBadge status={i.stock_status} /></TD>
                    <TD className="tabular text-right">{i.on_hand}</TD>
                    <TD className="tabular text-right font-medium">{i.available}</TD>
                    <TD className="tabular text-right text-muted-foreground">{i.threshold ?? "—"}</TD>
                    <TD>
                      <div className="flex gap-1">
                        <Input className="h-7 w-20" inputMode="numeric" aria-label={`Quantity for ${i.name}`} value={qtys[key(i)] ?? ""} onChange={(e) => setQtys({ ...qtys, [key(i)]: e.target.value.replace(/\D/g, "") })} />
                        <Button size="xs" variant="outline" disabled={!qtys[key(i)]} onClick={() => adjust(i, "increase")}><Plus /> Add</Button>
                        <Button size="xs" variant="ghost" disabled={!qtys[key(i)]} onClick={() => adjust(i, "set")}>Set</Button>
                      </div>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </Card>
      <Card className="overflow-hidden">
        <CardHeader><CardTitle>Recent inventory transactions</CardTitle></CardHeader>
        {!tx.data?.items.length ? <CardContent><p className="text-[13px] text-muted-foreground">No stock changes recorded yet.</p></CardContent> : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead><TR><TH>When</TH><TH>Product</TH><TH>Type</TH><TH className="text-right">Before</TH><TH className="text-right">Change</TH><TH className="text-right">After</TH><TH>By</TH><TH>Reason</TH></TR></THead>
                <TBody>
                  {tx.data.items.map((t) => (
                    <TR key={t.id}>
                      <TD className="whitespace-nowrap text-muted-foreground">{timeAgo(t.created_at)}</TD>
                      <TD className="max-w-[220px] truncate"><Link to={`/app/catalog/${t.product_id}`} className="hover:underline">{t.product_name}</Link>{t.variant_id && <Badge className="ml-1">variant</Badge>}</TD>
                      <TD><Badge>{humanize(t.transaction_type)}</Badge>{t.quantity_field === "reserved" && <Badge className="ml-1">reserved</Badge>}</TD>
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
            <div className="border-t"><Pagination page={txPage} pageSize={25} total={tx.data.total} onPage={setTxPage} /></div>
          </>
        )}
      </Card>
    </>
  );
}

/* ================================================================ Pricing */
export function PricingPage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<string | null>(null);
  const overview = useQuery({ queryKey: ["pricing-overview"], queryFn: commerce.pricingOverview });
  const products = useQuery({ queryKey: ["products", "pricing", search, page], queryFn: () => commerce.products({ q: search, page, page_size: 25, sort: "name_asc" }), placeholderData: keepPreviousData });
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: () => commerce.schedules("scheduled") });
  const history = useQuery({ queryKey: ["price-history-all"], queryFn: () => commerce.allPriceHistory({ page_size: 15 }) });
  const items = products.data?.items ?? [];
  const refresh = () => {
    for (const k of ["pricing-overview", "products", "schedules", "price-history-all"]) void qc.invalidateQueries({ queryKey: [k] });
  };
  const o = overview.data;
  return (
    <>
      <PageHeader title="Pricing" description="Manage prices independently of your website. All arithmetic uses exact decimals and every change is recorded." />
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Products" value={o?.products ?? "—"} />
        <Stat label="On sale" value={o?.price_status.on_sale ?? 0} />
        <Stat label="Without a price" value={o?.price_status.no_price ?? 0} />
        <Stat label="Scheduled changes" value={o?.scheduled_changes ?? 0} />
        <Stat label="Currencies" value={o ? Object.keys(o.currencies).join(", ") || "—" : "—"} hint={o?.missing_cost ? `${o.missing_cost} without cost price` : undefined} />
      </div>
      <Card className="mb-6 overflow-hidden">
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <div className="relative w-full sm:w-72">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input className="pl-8" placeholder="Search products" aria-label="Search products" value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
          </div>
          <div className="flex flex-wrap gap-2">
            <span className="self-center text-[13px] text-muted-foreground">{selected.size} selected</span>
            <Button size="sm" disabled={!selected.size} onClick={() => setBulk("price_adjust")}>Adjust prices</Button>
            <Button size="sm" variant="outline" disabled={!selected.size} onClick={() => setBulk("set_sale")}><Tag /> Set sale</Button>
            <Button size="sm" variant="outline" disabled={!selected.size} onClick={() => setBulk("clear_sale")}>End sale</Button>
            <Button size="sm" variant="outline" disabled={!selected.size} onClick={() => setBulk("schedule_price")}><CalendarClock /> Schedule</Button>
          </div>
        </CardHeader>
        {products.isLoading ? <div className="p-4"><Skeleton className="h-32" /></div> : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead><TR>
                  <TH className="w-8"><Checkbox aria-label="Select all" checked={items.length > 0 && items.every((p) => selected.has(p.id))} onCheckedChange={(v) => {
                    const n = new Set(selected); items.forEach((p) => (v ? n.add(p.id) : n.delete(p.id))); setSelected(n);
                  }} /></TH>
                  <TH>Product</TH><TH className="text-right">Regular</TH><TH className="text-right">Sale</TH><TH className="text-right">Selling now</TH><TH className="text-right">Cost</TH><TH>Status</TH>
                </TR></THead>
                <TBody>
                  {items.map((p) => (
                    <TR key={p.id}>
                      <TD><Checkbox aria-label={`Select ${p.name}`} checked={selected.has(p.id)} onCheckedChange={(v) => { const n = new Set(selected); if (v) n.add(p.id); else n.delete(p.id); setSelected(n); }} /></TD>
                      <TD className="max-w-[260px] truncate"><Link to={`/app/catalog/${p.id}`} className="font-medium hover:underline">{p.name}</Link><p className="font-mono text-[11.5px] text-muted-foreground">{p.sku ?? ""}</p></TD>
                      <TD className="tabular text-right">{money(p.pricing.regular_price, p.pricing.currency)}</TD>
                      <TD className="tabular text-right">{money(p.pricing.sale_price, p.pricing.currency)}</TD>
                      <TD className="tabular text-right font-medium">{money(p.pricing.effective_price, p.pricing.currency)}</TD>
                      <TD className="tabular text-right text-muted-foreground">{money(p.pricing.cost_price, p.pricing.currency)}</TD>
                      <TD><PriceStatusBadge status={p.pricing.price_status} /></TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <div className="border-t"><Pagination page={page} pageSize={25} total={products.data?.total ?? 0} onPage={setPage} /></div>
          </>
        )}
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="overflow-hidden">
          <CardHeader><div><CardTitle>Scheduled price changes</CardTitle><CardDescription>Applied by the scheduler at the set time; cancel any time before.</CardDescription></div></CardHeader>
          {!schedules.data?.items.length ? <CardContent><p className="text-[13px] text-muted-foreground">Nothing scheduled.</p></CardContent> : (
            <Table>
              <THead><TR><TH>Product</TH><TH>Change</TH><TH>When</TH><TH /></TR></THead>
              <TBody>
                {schedules.data.items.map((s) => (
                  <TR key={s.id}>
                    <TD className="max-w-[180px] truncate">{s.product_name}</TD>
                    <TD className="whitespace-nowrap">{humanize(s.price_type)} → {s.new_amount ? money(s.new_amount, s.currency) : "end sale"}</TD>
                    <TD className="whitespace-nowrap text-muted-foreground">{formatDate(s.effective_at)}</TD>
                    <TD><Button size="xs" variant="ghost" onClick={async () => { await commerce.cancelSchedule(s.id); toast.success("Cancelled"); refresh(); }}><X /> Cancel</Button></TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </Card>
        <Card className="overflow-hidden">
          <CardHeader><CardTitle>Recent price changes</CardTitle></CardHeader>
          {!history.data?.items.length ? <CardContent><p className="text-[13px] text-muted-foreground">No price changes yet.</p></CardContent> : (
            <Table>
              <THead><TR><TH>Product</TH><TH>Change</TH><TH>Type</TH><TH>When</TH></TR></THead>
              <TBody>
                {history.data.items.map((h) => (
                  <TR key={h.id}>
                    <TD className="max-w-[160px] truncate">{h.product_name}</TD>
                    <TD className="tabular whitespace-nowrap text-[12.5px]">{humanize(h.price_type)}: {money(h.old_amount, h.currency)} → {money(h.new_amount, h.currency)}</TD>
                    <TD><Badge>{humanize(h.change_type)}</Badge></TD>
                    <TD className="whitespace-nowrap text-muted-foreground">{timeAgo(h.created_at)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </Card>
      </div>
      <BulkDialog open={!!bulk} onOpenChange={(o) => !o && setBulk(null)} productIds={[...selected]} initialOperation={bulk ?? undefined}
        onDone={() => { refresh(); setSelected(new Set()); }} />
    </>
  );
}

/* ================================================================ Variants */
export function VariantsPage() {
  const [q, setQ] = useState("");
  const [stock, setStock] = useState("");
  const [page, setPage] = useState(1);
  const data = useQuery({ queryKey: ["variants-all", q, stock, page], queryFn: () => commerce.allVariants({ q, stock_status: stock, page, page_size: 50 }), placeholderData: keepPreviousData });
  return (
    <>
      <PageHeader title="Variants" description="Every live variant across your catalog. Open a product to create, edit or archive its variants." />
      <div className="mb-3 flex flex-wrap gap-2">
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input className="pl-8" aria-label="Search variants" placeholder="Search variant, SKU or product" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        </div>
        <NativeSelect aria-label="Stock" value={stock} onChange={(e) => { setStock(e.target.value); setPage(1); }}>
          <option value="">Any stock</option><option value="in_stock">In stock</option><option value="low_stock">Low stock</option>
          <option value="out_of_stock">Out of stock</option><option value="untracked">Not tracked</option>
        </NativeSelect>
      </div>
      <ErrorNote error={data.error} />
      <Card className="overflow-hidden">
        {data.isLoading ? <div className="p-4"><Skeleton className="h-32" /></div> : !data.data?.items.length ? (
          <div className="p-6"><EmptyState icon={<Layers />} title="No variants" description="Open a product and use the Variants tab to add sizes, colours or other options."
            action={<Button asChild size="sm" variant="outline"><Link to="/app/catalog">Go to catalog</Link></Button>} /></div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead><TR><TH>Product</TH><TH>Variant</TH><TH>SKU</TH><TH className="text-right">Price</TH><TH>Stock</TH><TH>Status</TH></TR></THead>
                <TBody>
                  {data.data.items.map((v) => (
                    <TR key={v.id}>
                      <TD className="max-w-[220px] truncate"><Link to={`/app/catalog/${v.product_id}`} className="font-medium hover:underline">{v.product_name}</Link></TD>
                      <TD>{v.name}<p className="text-[11.5px] text-muted-foreground">{Object.entries(v.options).map(([k, x]) => `${k}: ${x}`).join(" · ")}</p></TD>
                      <TD className="font-mono text-[12px]">{v.sku ?? "—"}</TD>
                      <TD className="tabular text-right">{money(v.effective_price, v.currency)}</TD>
                      <TD><StockBadge status={v.stock_status} available={v.inventory?.available} /></TD>
                      <TD>{v.is_enabled ? <Badge tone="success">Enabled</Badge> : <Badge>Disabled</Badge>}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <div className="border-t"><Pagination page={page} pageSize={50} total={data.data.total} onPage={setPage} /></div>
          </>
        )}
      </Card>
    </>
  );
}

