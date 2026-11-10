import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Check, Download, ImageOff, Layers, MoreHorizontal, Package, Plus, RotateCcw, Search, SlidersHorizontal, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { ApprovalBadge, EmptyState, ErrorNote, PageHeader, Pagination, QualityBadge } from "@/components/app/common";
import { BulkDialog, ConfirmDialog, LifecycleBadge, PriceStatusBadge, PublicationBadge, StockBadge, money } from "@/components/app/commerce";
import { Badge, Button, Card, Checkbox, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger, Tooltip } from "@/components/ui/overlays";
import { api } from "@/services/api";
import { commerce } from "@/services/commerce";
import type { CommerceProduct } from "@/types/commerce";
import { humanize, timeAgo } from "@/utils/format";

const PAGE_SIZE = 25;
const FILTER_KEYS = ["category_id", "brand", "approval_status", "publication_status", "lifecycle_status", "stock_status", "product_type",
  "quality_status", "origin", "price_min", "price_max"] as const;

function useDebounced<T>(value: T, ms = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function Catalog() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [search, setSearch] = useState(params.get("q") ?? "");
  const q = useDebounced(search);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState<string | null>(null);
  const [showFilters, setShowFilters] = useState(() => FILTER_KEYS.some((k) => params.get(k)));
  const [priceMin, setPriceMin] = useState(params.get("price_min") ?? "");
  const [priceMax, setPriceMax] = useState(params.get("price_max") ?? "");
  const [confirmDelete, setConfirmDelete] = useState<CommerceProduct | null>(null);

  const filters: Record<string, string | number> = { q, sort: params.get("sort") ?? "updated_desc", page: Number(params.get("page") ?? 1) };
  FILTER_KEYS.forEach((k) => {
    const v = params.get(k);
    if (v) filters[k] = v;
  });
  const setFilter = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    if (k !== "page") next.delete("page");
    setParams(next, { replace: true });
  };
  useEffect(() => {
    if ((params.get("q") ?? "") !== q) setFilter("q", q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);
  const activeFilters = FILTER_KEYS.filter((k) => params.get(k)).length;

  const products = useQuery({
    queryKey: ["products", filters],
    queryFn: () => commerce.products({ ...filters, page_size: PAGE_SIZE }),
    placeholderData: keepPreviousData,
  });
  const facets = useQuery({ queryKey: ["facets"], queryFn: commerce.facets });
  const categories = useQuery({ queryKey: ["categories-managed"], queryFn: () => commerce.categories() });
  const items = useMemo(() => products.data?.items ?? [], [products.data]);
  const allSelected = items.length > 0 && items.every((p) => selected.has(p.id));

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["products"] });
    void qc.invalidateQueries({ queryKey: ["overview"] });
    void qc.invalidateQueries({ queryKey: ["facets"] });
  };
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try {
      await fn();
      toast.success(msg);
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };
  const exportSelected = async () => {
    const job = await api.createExport({ format: "json", product_ids: [...selected], include_unapproved: true });
    await api.downloadExport(job);
    toast.success(`Exported ${job.product_count} product(s)`);
  };
  const sel = (label: string, key: string, options: [string, string][], all: string) => (
    <NativeSelect value={params.get(key) ?? ""} onChange={(e) => setFilter(key, e.target.value)} aria-label={label}>
      <option value="">{all}</option>
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </NativeSelect>
  );

  return (
    <>
      <PageHeader
        title="Product Catalog"
        description="Your managed catalog. Website data, merchant edits and AgentReady-managed prices and stock are tracked separately."
        actions={<Button size="sm" onClick={() => navigate("/app/catalog/new")}><Plus /> New product</Button>}
      />

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, SKU, brand, GTIN" className="pl-8" aria-label="Search products" />
        </div>
        {sel("Category", "category_id", [["__none__", "Uncategorised"], ...(categories.data?.items ?? []).map((c) => [c.id, `${c.path} (${c.product_count})`] as [string, string])], "All categories")}
        {sel("Stock", "stock_status", [["in_stock", "In stock"], ["low_stock", "Low stock"], ["out_of_stock", "Out of stock"], ["backorder", "Backorder"], ["untracked", "Not tracked"]], "Any stock")}
        {sel("Approval", "approval_status", [["needs_review", "Pending review"], ["approved", "Approved"], ["rejected", "Rejected"]], "Any approval")}
        <Button variant="outline" size="sm" onClick={() => setShowFilters((s) => !s)} aria-expanded={showFilters}>
          <SlidersHorizontal /> More filters{activeFilters ? ` (${activeFilters})` : ""}
        </Button>
        <NativeSelect value={String(filters.sort)} onChange={(e) => setFilter("sort", e.target.value)} aria-label="Sort">
          <option value="updated_desc">Recently updated</option>
          <option value="created_desc">Newest</option>
          <option value="name_asc">Name A–Z</option>
          <option value="price_asc">Price: low to high</option>
          <option value="price_desc">Price: high to low</option>
          <option value="stock_asc">Lowest stock</option>
          <option value="sku_asc">SKU</option>
          <option value="quality_asc">Lowest completeness</option>
        </NativeSelect>
      </div>
      {showFilters && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border bg-muted/20 p-2">
          {sel("Brand", "brand", (facets.data?.brands ?? []).filter((b) => b.value).map((b) => [b.value!, `${b.value} (${b.count})`]), "Any brand")}
          {sel("Lifecycle", "lifecycle_status", [["draft", "Draft"], ["active", "Active"], ["discontinued", "Discontinued"], ["archived", "Archived"], ["deleted", "Deleted"]], "Draft, active, discontinued")}
          {sel("Publication", "publication_status", [["published", "Published"], ["unpublished", "Unpublished"]], "Any publication")}
          {sel("Product type", "product_type", (facets.data?.product_types ?? []).filter((b) => b.value).map((b) => [b.value!, b.value!]), "Any type")}
          {sel("Quality", "quality_status", ["valid", "needs_review", "incomplete", "duplicate_candidate", "invalid"].map((s) => [s, humanize(s)]), "Any quality")}
          {sel("Source", "origin", [["website", "Website scan"], ["manual", "Created manually"], ["woocommerce", "WooCommerce"], ["shopify", "Shopify"]], "Any source")}
          <Input className="w-24" inputMode="decimal" placeholder="Min price" aria-label="Minimum price" value={priceMin}
            onChange={(e) => setPriceMin(e.target.value)} onBlur={() => setFilter("price_min", priceMin)} />
          <Input className="w-24" inputMode="decimal" placeholder="Max price" aria-label="Maximum price" value={priceMax}
            onChange={(e) => setPriceMax(e.target.value)} onBlur={() => setFilter("price_max", priceMax)} />
          {activeFilters > 0 && (
            <Button variant="ghost" size="sm" onClick={() => { const n = new URLSearchParams(); if (q) n.set("q", q); setPriceMin(""); setPriceMax(""); setParams(n, { replace: true }); }}>
              <X /> Clear filters
            </Button>
          )}
        </div>
      )}

      {selected.size > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border bg-muted/40 px-3 py-2 text-[13px]">
          <span className="font-medium">{selected.size} selected</span>
          <div className="flex-1" />
          <Button size="xs" onClick={() => setBulkOpen("approve")}><Check /> Approve</Button>
          <Button size="xs" variant="outline" onClick={() => setBulkOpen("price_adjust")}>Prices</Button>
          <Button size="xs" variant="outline" onClick={() => setBulkOpen("inventory_adjust")}>Stock</Button>
          <Button size="xs" variant="outline" onClick={() => setBulkOpen("assign_category")}>Category</Button>
          <Button size="xs" variant="outline" onClick={() => setBulkOpen("archive")}><Archive /> Archive</Button>
          <Button size="xs" variant="outline" onClick={() => setBulkOpen("publish")}><Layers /> More…</Button>
          <Button size="xs" variant="outline" onClick={exportSelected}><Download /> Export</Button>
          <Button size="xs" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
        </div>
      )}

      <ErrorNote error={products.error} />
      <Card className="overflow-hidden">
        {products.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-10" />)}</div>
        ) : items.length === 0 ? (
          <div className="p-6">
            <EmptyState
              icon={<Package />}
              title="No products found"
              description={q || activeFilters ? "Try clearing the search or filters." : "Scan a website, connect a store, or create a product manually."}
              action={!q && !activeFilters ? (
                <div className="flex gap-2">
                  <Button asChild size="sm"><Link to="/app/catalog/new"><Plus /> New product</Link></Button>
                  <Button asChild size="sm" variant="outline"><Link to="/app/connect">Connect website</Link></Button>
                </div>
              ) : undefined}
            />
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <TR className="hover:bg-transparent">
                    <TH className="w-8">
                      <Checkbox checked={allSelected} onCheckedChange={(v) => setSelected(v ? new Set(items.map((p) => p.id)) : new Set())} aria-label="Select all" />
                    </TH>
                    <TH className="w-12">Image</TH>
                    <TH>Product</TH>
                    <TH>SKU</TH>
                    <TH>Category</TH>
                    <TH className="text-right">Price</TH>
                    <TH>Stock</TH>
                    <TH>Status</TH>
                    <TH>Approval</TH>
                    <TH>Updated</TH>
                    <TH className="w-10" />
                  </TR>
                </THead>
                <TBody>
                  {items.map((p) => (
                    <TR key={p.id} data-state={selected.has(p.id) ? "selected" : undefined} className="cursor-pointer" onClick={() => navigate(`/app/catalog/${p.id}`)}>
                      <TD onClick={(e) => e.stopPropagation()}>
                        <Checkbox
                          checked={selected.has(p.id)}
                          onCheckedChange={(v) => {
                            const next = new Set(selected);
                            if (v) next.add(p.id);
                            else next.delete(p.id);
                            setSelected(next);
                          }}
                          aria-label={`Select ${p.name}`}
                        />
                      </TD>
                      <TD>
                        {p.images[0] ? (
                          <img src={p.images[0].url} alt={p.images[0].alt ?? ""} loading="lazy" referrerPolicy="no-referrer" className="h-9 w-9 rounded border object-cover"
                            onError={(e) => ((e.target as HTMLImageElement).style.visibility = "hidden")} />
                        ) : (
                          <div className="grid h-9 w-9 place-items-center rounded border bg-muted text-muted-foreground"><ImageOff className="h-3.5 w-3.5" /></div>
                        )}
                      </TD>
                      <TD className="max-w-[240px]">
                        <p className="truncate font-medium">{p.name ?? <span className="text-muted-foreground">Unnamed</span>}</p>
                        <p className="truncate text-[12px] text-muted-foreground">
                          {[p.brand, p.variants.length ? `${p.variants.filter((v) => !v.archived_at).length} variants` : null, p.origin !== "website" ? humanize(p.origin) : null].filter(Boolean).join(" · ") || " "}
                        </p>
                      </TD>
                      <TD className="font-mono text-[12px] text-muted-foreground">{p.sku ?? "—"}</TD>
                      <TD className="max-w-[160px] truncate text-muted-foreground" title={p.category ?? ""}>{p.category ?? "—"}</TD>
                      <TD className="tabular whitespace-nowrap text-right">
                        <div>{money(p.pricing.effective_price, p.pricing.currency)}</div>
                        {p.pricing.price_status === "on_sale" && <div className="text-[11.5px] text-muted-foreground line-through">{money(p.pricing.regular_price, p.pricing.currency)}</div>}
                        {p.pricing.price_status !== "regular" && p.pricing.price_status !== "on_sale" && <PriceStatusBadge status={p.pricing.price_status} />}
                      </TD>
                      <TD><StockBadge status={p.stock_status} available={p.inventory.available} /></TD>
                      <TD>
                        <div className="flex flex-col items-start gap-1">
                          <LifecycleBadge status={p.lifecycle_status} />
                          {p.deleted_at ? <Badge tone="danger">Deleted</Badge> : <PublicationBadge status={p.publication_status} />}
                        </div>
                      </TD>
                      <TD>
                        <div className="flex flex-col items-start gap-1">
                          <ApprovalBadge status={p.approval_status} />
                          <div className="flex items-center gap-1">
                            <QualityBadge status={p.quality_status} />
                            {!!p.open_issues && <Tooltip content={`${p.open_issues} open issue(s)`}><span className="tabular text-[11px] text-muted-foreground">{p.open_issues}</span></Tooltip>}
                          </div>
                        </div>
                      </TD>
                      <TD className="whitespace-nowrap text-muted-foreground">{timeAgo(p.updated_at)}</TD>
                      <TD onClick={(e) => e.stopPropagation()}>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon" className="h-7 w-7" aria-label="Actions"><MoreHorizontal /></Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onSelect={() => navigate(`/app/catalog/${p.id}`)}><Package /> View & edit</DropdownMenuItem>
                            {p.approval_status !== "approved" && !p.deleted_at && (
                              <DropdownMenuItem onSelect={() => act(() => commerce.updateProduct(p.id, { approval_status: "approved" }), "Approved")}><Check /> Approve</DropdownMenuItem>
                            )}
                            {p.lifecycle_status === "archived" || p.deleted_at ? (
                              <DropdownMenuItem onSelect={() => act(() => commerce.restore(p.id), "Restored")}><RotateCcw /> Restore</DropdownMenuItem>
                            ) : (
                              <DropdownMenuItem onSelect={() => act(() => commerce.archive(p.id), "Archived")}><Archive /> Archive</DropdownMenuItem>
                            )}
                            {!p.deleted_at && (
                              <>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem destructive onSelect={() => setConfirmDelete(p)}><Trash2 /> Delete</DropdownMenuItem>
                              </>
                            )}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
            <div className="border-t">
              <Pagination page={Number(filters.page)} pageSize={PAGE_SIZE} total={products.data?.total ?? 0} onPage={(pg) => setFilter("page", String(pg))} />
            </div>
          </>
        )}
      </Card>

      <BulkDialog open={!!bulkOpen} onOpenChange={(o) => !o && setBulkOpen(null)} productIds={[...selected]} initialOperation={bulkOpen ?? undefined}
        categories={categories.data?.items ?? []} onDone={() => setSelected(new Set())} />
      <ConfirmDialog
        open={!!confirmDelete}
        onOpenChange={(o) => !o && setConfirmDelete(null)}
        title={`Delete "${confirmDelete?.name}"?`}
        description="The product leaves your catalog and feeds. Its history is kept and you can restore it from the Deleted filter."
        confirmLabel="Delete product"
        destructive
        onConfirm={async () => {
          await commerce.remove(confirmDelete!.id);
          toast("Product deleted", { description: "Restorable from Lifecycle → Deleted" });
          refresh();
        }}
      />
    </>
  );
}
