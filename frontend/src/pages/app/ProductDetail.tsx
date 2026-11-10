import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFieldArray, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Archive, ArrowLeft, Check, CheckCircle2, ExternalLink, EyeOff, Globe2, Pencil, Plus, RotateCcw, ThumbsDown, Trash2, X } from "lucide-react";
import { ConfirmDialog, LifecycleBadge, PublicationBadge, StockBadge, money } from "@/components/app/commerce";
import { commerce } from "@/services/commerce";
import { ProductDistributionPanel } from "@/components/app/distribution";
import type { CommerceProduct } from "@/types/commerce";
import { HistorySection, ImagesSection, InventorySection, OverviewSection, PricingSection, SeoSection, VariantsSection } from "@/pages/app/product/sections";
import { toast } from "sonner";
import { ApprovalBadge, ErrorNote, MethodBadge, QualityBadge, SeverityBadge } from "@/components/app/common";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Field, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Tabs, TabsContent, TabsList, TabsTrigger, Tooltip } from "@/components/ui/overlays";
import { api } from "@/services/api";
import type { Evidence, Issue, ProductDetail } from "@/types/api";
import { displayValue, formatDate, humanize, timeAgo } from "@/utils/format";

const AVAILABILITY = ["InStock", "OutOfStock", "PreOrder", "BackOrder", "Discontinued", "LimitedAvailability", "SoldOut"];

export default function ProductDetailPage() {
  const { productId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [tab, setTab] = useState("overview");
  const { data: p, isLoading, error } = useQuery({ queryKey: ["product", productId], queryFn: () => api.product(productId) });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["product", productId] });
    void qc.invalidateQueries({ queryKey: ["products"] });
    void qc.invalidateQueries({ queryKey: ["overview"] });
    void qc.invalidateQueries({ queryKey: ["issues"] });
    void qc.invalidateQueries({ queryKey: ["history", productId] });
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

  const setApproval = async (approval_status: string) => {
    try {
      await api.updateProduct(productId, { approval_status });
      toast.success(approval_status === "approved" ? "Product approved" : "Status updated");
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  if (isLoading) return <div className="space-y-3"><Skeleton className="h-8 w-80" /><Skeleton className="h-72" /></div>;
  if (error || !p) return <ErrorNote error={error ?? "Product not found"} />;

  const cp = p as unknown as CommerceProduct;
  const openIssues = p.issues.filter((i) => i.status === "open");
  const blocking = openIssues.filter((i) => i.severity === "error").length;
  const evidenceByField = p.evidence.reduce<Record<string, Evidence[]>>((acc, e) => {
    const root = e.field.startsWith("attributes.") ? e.field : e.field.split(".")[0];
    (acc[root] ||= []).push(e);
    return acc;
  }, {});

  return (
    <>
      <Link to="/app/catalog" className="mb-4 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Catalog
      </Link>
      <div className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex gap-4">
          {p.images[0] && (
            <img src={p.images[0].url} alt={p.images[0].alt ?? ""} referrerPolicy="no-referrer" className="h-16 w-16 rounded-md border object-cover" />
          )}
          <div>
            <h1 className="text-xl font-semibold">{p.name ?? "Unnamed product"}</h1>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <LifecycleBadge status={cp.lifecycle_status} />
              {cp.deleted_at ? <Badge tone="danger">Deleted</Badge> : <PublicationBadge status={cp.publication_status} />}
              <ApprovalBadge status={p.approval_status} />
              <QualityBadge status={p.quality_status} />
              <StockBadge status={cp.stock_status} available={cp.inventory.available} />
              <span className="tabular text-[13px] font-medium">{money(cp.pricing.effective_price, cp.pricing.currency)}</span>
              <Badge>v{p.version}</Badge>

              <span className="text-[12px] text-muted-foreground">Updated {timeAgo(p.updated_at)}</span>
            </div>
          </div>
        </div>
        {cp.deleted_at ? (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => act(() => commerce.restore(p.id), "Product restored")}><RotateCcw /> Restore product</Button>
          </div>
        ) : (
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditing(true)}><Pencil /> Edit details</Button>
          {cp.approval_status === "approved" && cp.publication_status === "published" && (
            <Button variant="outline" size="sm" onClick={() => act(() => commerce.updateProduct(p.id, { publication_status: "unpublished" }), "Unpublished")}><EyeOff /> Unpublish</Button>
          )}
          {cp.approval_status === "approved" && cp.publication_status === "unpublished" && (
            <Button variant="outline" size="sm" onClick={() => act(() => commerce.updateProduct(p.id, { publication_status: "published" }), "Published")}><Globe2 /> Publish</Button>
          )}
          {cp.lifecycle_status === "archived" ? (
            <Button variant="outline" size="sm" onClick={() => act(() => commerce.restore(p.id), "Restored")}><RotateCcw /> Restore</Button>
          ) : (
            <Button variant="outline" size="sm" onClick={() => act(() => commerce.archive(p.id), "Archived")}><Archive /> Archive</Button>
          )}
          {p.approval_status !== "rejected" && <Button variant="outline" size="sm" onClick={() => setApproval("rejected")}><ThumbsDown /> Reject</Button>}
          {p.approval_status !== "needs_review" && <Button variant="outline" size="sm" onClick={() => setApproval("needs_review")}><RotateCcw /> Back to review</Button>}
          {p.approval_status !== "approved" && (
            <Tooltip content={blocking ? `Resolve ${blocking} blocking error(s) first` : "Publish to catalog API and exports"}>
              <span><Button size="sm" disabled={blocking > 0} onClick={() => setApproval("approved")}><Check /> Approve</Button></span>
            </Tooltip>
          )}
          <Button variant="ghost" size="sm" aria-label="Delete" onClick={() => setConfirmDelete(true)}><Trash2 /></Button>
        </div>
        )}
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <div className="overflow-x-auto">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="details">Basic info</TabsTrigger>
          <TabsTrigger value="pricing">Pricing</TabsTrigger>
          <TabsTrigger value="inventory">Inventory</TabsTrigger>
          <TabsTrigger value="variants">Variants <Badge className="ml-1">{cp.variants.filter((v) => !v.archived_at).length}</Badge></TabsTrigger>
          <TabsTrigger value="specs">Specifications</TabsTrigger>
          <TabsTrigger value="images">Images</TabsTrigger>
          <TabsTrigger value="seo">SEO & structured data</TabsTrigger>
          <TabsTrigger value="evidence">Source evidence <Badge className="ml-1">{p.evidence.length}</Badge></TabsTrigger>
          <TabsTrigger value="issues">Issues <Badge className="ml-1" tone={openIssues.length ? "warning" : "neutral"}>{openIssues.length}</Badge></TabsTrigger>
          <TabsTrigger value="distribution">Distribution</TabsTrigger>
          <TabsTrigger value="history">Change history</TabsTrigger>
        </TabsList>
        </div>
        <TabsContent value="overview"><OverviewSection p={cp} /></TabsContent>
        <TabsContent value="pricing"><PricingSection p={cp} /></TabsContent>
        <TabsContent value="inventory"><InventorySection p={cp} /></TabsContent>
        <TabsContent value="variants"><VariantsSection p={cp} /></TabsContent>
        <TabsContent value="images"><ImagesSection p={cp} /></TabsContent>
        <TabsContent value="seo"><SeoSection p={cp} /></TabsContent>
        <TabsContent value="history"><HistorySection p={cp} /></TabsContent>
        <TabsContent value="distribution"><ProductDistributionPanel productId={cp.id} /></TabsContent>

        <TabsContent value="details">
          <div className="grid gap-4 lg:grid-cols-[1.5fr_1fr]">
            <Card>
              <CardHeader><CardTitle>Product information</CardTitle></CardHeader>
              <CardContent>
                <dl className="divide-y">
                  {(
                    [
                      ["name", "Name", p.name],
                      ["price", "Regular price", money(cp.pricing.regular_price, cp.pricing.currency)],
                      ["product_type", "Product type", cp.product_type],
                      ["sku", "SKU", p.sku],
                      ["brand", "Brand", p.brand],
                      ["manufacturer", "Manufacturer", p.manufacturer],
                      ["category", "Category", p.category ? `${p.category}${p.category_inferred ? " (inferred)" : ""}` : null],
                      ["availability", "Availability", p.availability],
                      ["gtin", "GTIN", p.gtin],
                      ["mpn", "MPN", p.mpn],
                      ["material", "Material", p.material],
                      ["weight", "Weight", p.weight ? displayValue(p.weight) : null],
                      ["dimensions", "Dimensions", p.dimensions ? displayValue(p.dimensions) : null],
                      ["product_url", "Product URL", p.product_url],
                    ] as const
                  ).map(([key, label, value]) => (
                    <div key={key} className="grid grid-cols-[140px_1fr_auto] items-start gap-3 py-2 text-[13px]">
                      <dt className="text-muted-foreground">{label}</dt>
                      <dd className="min-w-0 break-words">{value ?? <span className="text-muted-foreground">—</span>}</dd>
                      <dd>
                        {p.manual_overrides.includes(key) ? (
                          <Badge tone="solid">edited</Badge>
                        ) : evidenceByField[key === "price" ? "price" : key]?.length ? (
                          <Badge tone={evidenceByField[key][0].verified_in_source === false ? "warning" : "neutral"}>
                            {evidenceByField[key].length} source{evidenceByField[key].length > 1 ? "s" : ""}
                          </Badge>
                        ) : null}
                      </dd>
                    </div>
                  ))}
                </dl>
                {p.description && (
                  <div className="mt-4 border-t pt-4">
                    <p className="mb-1 text-[12px] font-medium text-muted-foreground">Description</p>
                    <p className="whitespace-pre-line text-[13px] leading-6">{p.description}</p>
                  </div>
                )}
              </CardContent>
            </Card>
            <div className="space-y-4">
              <Card>
                <CardHeader><CardTitle>Merchant-managed fields</CardTitle></CardHeader>
                <CardContent className="text-[13px] text-muted-foreground">
                  {p.manual_overrides.length ? (
                    <>These fields are managed in AgentReady. Rescans never overwrite them; a website change creates a sync conflict instead: <span className="text-foreground">{p.manual_overrides.join(", ")}</span></>
                  ) : "None yet. Edited fields are kept on future rescans."}
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="specs">
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="space-y-4">
              <Card>
                <CardHeader className="flex-row items-center justify-between"><CardTitle>Specifications</CardTitle><Button size="xs" variant="outline" onClick={() => setEditing(true)}><Pencil /> Edit</Button></CardHeader>
                <CardContent>
                  {p.attributes.length === 0 ? (
                    <p className="text-[13px] text-muted-foreground">No attributes extracted.</p>
                  ) : (
                    <dl className="divide-y text-[13px]">
                      {p.attributes.map((a) => (
                        <div key={a.key} className="flex items-center justify-between gap-3 py-1.5">
                          <dt className="text-muted-foreground">{a.label}</dt>
                          <dd className="tabular text-right">
                            {a.value ?? "—"} {a.unit && <span className="text-muted-foreground">{a.unit}</span>}
                            {a.source === "merchant" && <Badge tone="solid" className="ml-1.5">edited</Badge>}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="evidence">
          <div className="space-y-4">
            <EvidenceTable evidence={p.evidence} />
            <SourcePage page={p.source_page} />
          </div>
        </TabsContent>
        <TabsContent value="issues"><IssueList issues={p.issues} onChange={refresh} /></TabsContent>
      </Tabs>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete "${p.name}"?`}
        description="It leaves your catalog and feeds immediately. Price, stock and change history are kept, and you can restore it."
        confirmLabel="Delete product"
        destructive
        onConfirm={async () => {
          await commerce.remove(p.id);
          toast("Product deleted", { description: "You can restore it from the catalog's Deleted filter." });
          refresh();
          navigate("/app/catalog");
        }}
      />

      <EditProductDialog product={p} open={editing} onOpenChange={setEditing} onSaved={refresh} />
    </>
  );
}

function EvidenceTable({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) return <p className="text-[13px] text-muted-foreground">No evidence recorded.</p>;
  return (
    <Card className="overflow-hidden">
      <div className="border-b bg-muted/30 px-4 py-2.5 text-[12.5px] text-muted-foreground">
        Every value links to the source text it came from. AI-provided values are checked against the page;
        "not found in source" means the quote could not be located verbatim and needs your review. Confidence is a heuristic, not verification.
      </div>
      <Table>
        <THead><TR><TH>Field</TH><TH>Value</TH><TH>Source text</TH><TH>Method</TH><TH>Source</TH><TH>Check</TH></TR></THead>
        <TBody>
          {evidence.map((e) => (
            <TR key={e.id} className="align-top">
              <TD className="font-mono text-[12px]">{e.field}</TD>
              <TD className="max-w-[180px] break-words">{displayValue(e.value)}{e.inferred && <Badge className="ml-1">inferred</Badge>}</TD>
              <TD className="max-w-[300px]">
                <p className="break-words font-mono text-[11.5px]">{e.source_value ?? "—"}</p>
                {e.source_fragment && e.source_fragment !== e.source_value && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-[11.5px] text-muted-foreground">Context</summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted/50 p-2 text-[11px]">{e.source_fragment}</pre>
                  </details>
                )}
              </TD>
              <TD>
                <div className="flex flex-col items-start gap-1">
                  <MethodBadge method={e.extraction_method} model={e.model_id} />
                  <span className="text-[11px] text-muted-foreground">{humanize(e.source_type)}{e.prompt_version ? ` · ${e.prompt_version}` : ""}</span>
                </div>
              </TD>
              <TD className="max-w-[180px]">
                {e.source_url ? (
                  <a href={e.source_url} target="_blank" rel="noopener noreferrer" title={e.source_url} className="flex min-w-0 items-center gap-1 font-mono text-[11.5px] text-muted-foreground hover:text-foreground">
                    <span className="truncate">{new URL(e.source_url).pathname}</span> <ExternalLink className="h-3 w-3 shrink-0" />
                  </a>
                ) : "—"}
              </TD>
              <TD>
                {e.verified_in_source === true && <Badge tone="success">found in source</Badge>}
                {e.verified_in_source === false && <Badge tone="warning">not found in source</Badge>}
                {e.confidence !== null && <p className="mt-1 text-[11px] text-muted-foreground">est. {Math.round(e.confidence * 100)}%</p>}
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </Card>
  );
}

export function IssueList({ issues, onChange }: { issues: Issue[]; onChange: () => void }) {
  if (!issues.length) return <p className="flex items-center gap-2 text-[13px] text-muted-foreground"><CheckCircle2 className="h-4 w-4 text-success" />No validation issues.</p>;
  const act = async (i: Issue, action: "resolve" | "dismiss" | "reopen") => {
    await api.resolveIssue(i.id, action);
    toast.success(action === "reopen" ? "Issue reopened" : action === "resolve" ? "Marked resolved" : "Dismissed");
    onChange();
  };
  return (
    <div className="space-y-2">
      {issues.map((i) => (
        <div key={i.id} className="flex flex-col gap-2 rounded-md border px-4 py-3 sm:flex-row sm:items-center">
          <div className="flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <SeverityBadge severity={i.severity} />
              <span className="font-mono text-[12px] text-muted-foreground">{i.code}</span>
              {i.status !== "open" && <Badge>{i.status}</Badge>}
            </div>
            <p className="mt-1 text-[13px]">{i.message}</p>
            {Array.isArray(i.details.values) && <p className="mt-0.5 font-mono text-[11.5px] text-muted-foreground">Values: {(i.details.values as string[]).join(" vs ")}</p>}
            {typeof i.details.other_product_id === "string" && (
              <Link to={`/app/catalog/${i.details.other_product_id}`} className="text-[12px] underline-offset-4 hover:underline">Compare with other product</Link>
            )}
            {i.resolution_note && <p className="mt-0.5 text-[12px] text-muted-foreground">Note: {i.resolution_note}</p>}
          </div>
          <div className="flex gap-1.5">
            {i.status === "open" ? (
              <>
                <Button size="xs" variant="outline" onClick={() => act(i, "resolve")}><Check /> Resolve</Button>
                <Button size="xs" variant="ghost" onClick={() => act(i, "dismiss")}><X /> Dismiss</Button>
              </>
            ) : (
              <Button size="xs" variant="ghost" onClick={() => act(i, "reopen")}><RotateCcw /> Reopen</Button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function SourcePage({ page }: { page: ProductDetail["source_page"] }) {
  if (!page) return <p className="text-[13px] text-muted-foreground">The source page record is not available (it may belong to a deleted scan).</p>;
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <Card>
        <CardHeader>
          <div className="min-w-0">
            <CardTitle className="truncate">{page.title ?? page.url}</CardTitle>
            <a href={page.url} target="_blank" rel="noopener noreferrer" className="mt-0.5 inline-flex items-center gap-1 break-all font-mono text-[12px] text-muted-foreground hover:text-foreground">
              {page.url} <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        </CardHeader>
        <CardContent>
          <p className="mb-2 text-[12px] text-muted-foreground">Text captured on {formatDate(page.fetched_at)} (scripts and markup removed)</p>
          <pre className="max-h-[480px] overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 font-mono text-[12px] leading-5">{page.text_excerpt || "No text captured."}</pre>
        </CardContent>
      </Card>
      <Card className="h-fit">
        <CardHeader><CardTitle>Why this page was treated as a {page.page_type} page</CardTitle></CardHeader>
        <CardContent>
          <ul className="space-y-1 font-mono text-[11.5px] text-muted-foreground">
            {(page.signals?.reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ edit dialog */
const optional = z.string().trim().max(500).optional().or(z.literal(""));
const editSchema = z.object({
  name: z.string().trim().min(1, "Name is required").max(500),
  sku: optional,
  brand: optional,
  manufacturer: optional,
  category: optional,
  description: z.string().max(20000).optional(),
  price_amount: z.string().trim().refine((v) => v === "" || (/^\d+(\.\d{1,2})?$/.test(v) && Number(v) >= 0), "Enter a price like 450 or 12.99"),
  currency: z.string().trim().refine((v) => v === "" || /^[A-Za-z]{3}$/.test(v), "3-letter ISO code, e.g. INR"),
  availability: z.string().optional(),
  gtin: z.string().trim().refine((v) => v === "" || /^\d{8,14}$/.test(v), "8–14 digits"),
  mpn: optional,
  material: optional,
  attributes: z.array(z.object({ label: z.string().trim().min(1, "Required"), value: z.string().optional(), unit: z.string().optional() })),
});
type EditValues = z.infer<typeof editSchema>;

function EditProductDialog({ product: p, open, onOpenChange, onSaved }: { product: ProductDetail; open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void }) {
  const [error, setError] = useState<unknown>(null);
  const defaults: EditValues = {
    name: p.name ?? "", sku: p.sku ?? "", brand: p.brand ?? "", manufacturer: p.manufacturer ?? "", category: p.category ?? "",
    description: p.description ?? "", price_amount: p.price?.amount != null ? String(p.price.amount) : "", currency: p.price?.currency ?? "",
    availability: p.availability ?? "", gtin: p.gtin ?? "", mpn: p.mpn ?? "", material: p.material ?? "",
    attributes: p.attributes.map((a) => ({ label: a.label, value: a.value ?? "", unit: a.unit ?? "" })),
  };
  const form = useForm<EditValues>({ resolver: zodResolver(editSchema), values: defaults });
  const attrs = useFieldArray({ control: form.control, name: "attributes" });
  const { errors, isSubmitting, dirtyFields } = form.formState;

  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    const patch: Record<string, unknown> = {};
    const nul = (s?: string) => (s && s.trim() ? s.trim() : null);
    (Object.keys(dirtyFields) as (keyof EditValues)[]).forEach((k) => {
      if (k === "attributes") patch.attributes = v.attributes.map((a) => ({ label: a.label, value: nul(a.value), unit: nul(a.unit) }));
      else if (k === "price_amount") patch.price_amount = v.price_amount === "" ? null : v.price_amount;
      else patch[k] = nul(v[k] as string);
    });
    if (!Object.keys(patch).length) return onOpenChange(false);
    try {
      await api.updateProduct(p.id, patch);
      toast.success("Changes saved", { description: "Edited fields are preserved on future rescans." });
      onSaved();
      onOpenChange(false);
    } catch (e) {
      setError(e);
    }
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit product</DialogTitle>
          <DialogDescription>Edited fields are marked as merchant corrections and are kept when the website is rescanned.</DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <ErrorNote error={error} />
          <Field label="Name" htmlFor="e-name" error={errors.name?.message}><Input id="e-name" {...form.register("name")} /></Field>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Price" htmlFor="e-price" error={errors.price_amount?.message}><Input id="e-price" inputMode="decimal" {...form.register("price_amount")} /></Field>
            <Field label="Currency" htmlFor="e-cur" error={errors.currency?.message}><Input id="e-cur" maxLength={3} className="uppercase" {...form.register("currency")} /></Field>
            <Field label="Availability" htmlFor="e-av">
              <NativeSelect id="e-av" className="w-full" {...form.register("availability")}>
                <option value="">Not stated</option>
                {AVAILABILITY.map((a) => <option key={a} value={a}>{a}</option>)}
              </NativeSelect>
            </Field>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="SKU" htmlFor="e-sku"><Input id="e-sku" {...form.register("sku")} /></Field>
            <Field label="GTIN" htmlFor="e-gtin" error={errors.gtin?.message}><Input id="e-gtin" inputMode="numeric" {...form.register("gtin")} /></Field>
            <Field label="MPN" htmlFor="e-mpn"><Input id="e-mpn" {...form.register("mpn")} /></Field>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Brand" htmlFor="e-brand"><Input id="e-brand" {...form.register("brand")} /></Field>
            <Field label="Manufacturer" htmlFor="e-mfr"><Input id="e-mfr" {...form.register("manufacturer")} /></Field>
            <Field label="Material" htmlFor="e-mat"><Input id="e-mat" {...form.register("material")} /></Field>
          </div>
          <Field label="Category" htmlFor="e-cat"><Input id="e-cat" {...form.register("category")} /></Field>
          <Field label="Description" htmlFor="e-desc"><Textarea id="e-desc" rows={4} {...form.register("description")} /></Field>
          <div>
            <div className="mb-2 flex items-center justify-between">
              <p className="text-[13px] font-medium">Specifications</p>
              <Button type="button" size="xs" variant="outline" onClick={() => attrs.append({ label: "", value: "", unit: "" })}><Plus /> Add</Button>
            </div>
            <div className="space-y-2">
              {attrs.fields.map((f, i) => (
                <div key={f.id} className="grid grid-cols-[1fr_1fr_90px_auto] gap-2">
                  <Input placeholder="Name" {...form.register(`attributes.${i}.label`)} aria-invalid={!!errors.attributes?.[i]?.label} />
                  <Input placeholder="Value" {...form.register(`attributes.${i}.value`)} />
                  <Input placeholder="Unit" {...form.register(`attributes.${i}.unit`)} />
                  <Button type="button" variant="ghost" size="icon" onClick={() => attrs.remove(i)} aria-label="Remove attribute"><X /></Button>
                </div>
              ))}
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" loading={isSubmitting}>Save changes</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
