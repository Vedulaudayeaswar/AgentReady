import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFieldArray, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ArrowLeft, Plus, X } from "lucide-react";
import { toast } from "sonner";
import { ErrorNote, PageHeader } from "@/components/app/common";
import { Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, Label, NativeSelect, Textarea } from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { commerce } from "@/services/commerce";

const AVAILABILITY = ["InStock", "OutOfStock", "PreOrder", "BackOrder", "Discontinued", "LimitedAvailability"];
const opt = z.string().trim().max(500).optional().or(z.literal(""));
const amount = z.string().trim().refine((v) => v === "" || (/^\d+(\.\d{1,2})?$/.test(v) && Number(v) > 0), "Enter an amount like 499 or 12.99");
const whole = z.string().trim().refine((v) => v === "" || /^\d+$/.test(v), "Whole number");

const schema = z.object({
  name: z.string().trim().min(1, "Name is required").max(500),
  description: z.string().max(20000).optional(),
  brand: opt, manufacturer: opt, sku: opt, mpn: opt, material: opt, product_type: opt,
  gtin: z.string().trim().refine((v) => v === "" || /^\d{8,14}$/.test(v), "8–14 digits"),
  category_id: z.string().optional(),
  regular_price: amount, sale_price: amount, cost_price: z.string().trim().refine((v) => v === "" || /^\d+(\.\d{1,2})?$/.test(v), "Amount"),
  currency: z.string().trim().refine((v) => v === "" || /^[A-Za-z]{3}$/.test(v), "3-letter ISO code, e.g. INR"),
  availability: z.string().optional(),
  track_inventory: z.boolean(),
  stock_quantity: whole, low_stock_threshold: whole,
  backorder_policy: z.enum(["deny", "allow"]),
  weight: z.string().trim().max(40).optional(), weight_unit: z.string().trim().max(20).optional(),
  dimensions: z.string().trim().max(40).optional(), dimensions_unit: z.string().trim().max(20).optional(),
  requires_shipping: z.boolean(), shipping_class: opt, lead_time_days: whole,
  product_url: z.string().trim().refine((v) => v === "" || /^https?:\/\/\S+$/.test(v), "Must start with http:// or https://"),
  images: z.array(z.object({ url: z.string().trim().refine((v) => /^https?:\/\/\S+$/.test(v), "http(s) URL"), alt: z.string().optional() })),
  attributes: z.array(z.object({ label: z.string().trim().min(1, "Required"), value: z.string().optional(), unit: z.string().optional() })),
  seo_title: z.string().max(200).optional(), seo_description: z.string().max(500).optional(),
  lifecycle_status: z.enum(["draft", "active"]),
}).refine((v) => !(v.sale_price && !v.regular_price), { path: ["sale_price"], message: "Set a regular price first" })
  .refine((v) => !(v.sale_price && v.regular_price && Number(v.sale_price) >= Number(v.regular_price)), { path: ["sale_price"], message: "Must be lower than the regular price" });
type Values = z.infer<typeof schema>;

export default function ProductCreate() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { user } = useAuth();
  const [error, setError] = useState<unknown>(null);
  const categories = useQuery({ queryKey: ["categories-managed"], queryFn: () => commerce.categories() });
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: "", description: "", brand: "", manufacturer: "", sku: "", mpn: "", material: "", product_type: "", gtin: "", category_id: "",
      regular_price: "", sale_price: "", cost_price: "", currency: user?.business.default_currency ?? "", availability: "",
      track_inventory: false, stock_quantity: "", low_stock_threshold: "", backorder_policy: "deny",
      weight: "", weight_unit: "kg", dimensions: "", dimensions_unit: "cm", requires_shipping: true, shipping_class: "", lead_time_days: "",
      product_url: "", images: [], attributes: [], seo_title: "", seo_description: "", lifecycle_status: "draft",
    },
  });
  const images = useFieldArray({ control: form.control, name: "images" });
  const attrs = useFieldArray({ control: form.control, name: "attributes" });
  const { errors, isSubmitting } = form.formState;
  const track = form.watch("track_inventory");

  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    const s = (x?: string) => (x && x.trim() ? x.trim() : undefined);
    const n = (x?: string) => (x && x.trim() ? Number(x) : undefined);
    const body: Record<string, unknown> = {
      name: v.name.trim(), description: s(v.description), brand: s(v.brand), manufacturer: s(v.manufacturer), sku: s(v.sku), mpn: s(v.mpn),
      material: s(v.material), product_type: s(v.product_type), gtin: s(v.gtin), category_id: s(v.category_id),
      regular_price: s(v.regular_price), sale_price: s(v.sale_price), cost_price: s(v.cost_price), currency: s(v.currency)?.toUpperCase(),
      availability: track ? undefined : s(v.availability), track_inventory: v.track_inventory,
      stock_quantity: v.track_inventory ? n(v.stock_quantity) : undefined, low_stock_threshold: v.track_inventory ? n(v.low_stock_threshold) : undefined,
      backorder_policy: v.backorder_policy,
      weight: s(v.weight) ? { value: v.weight!.trim(), unit: s(v.weight_unit) } : undefined,
      dimensions: s(v.dimensions) ? { value: v.dimensions!.trim(), unit: s(v.dimensions_unit) } : undefined,
      shipping: { requires_shipping: v.requires_shipping, shipping_class: s(v.shipping_class), lead_time_days: n(v.lead_time_days) },
      product_url: s(v.product_url), images: v.images.map((i) => ({ url: i.url.trim(), alt: s(i.alt) })),
      attributes: v.attributes.map((a) => ({ label: a.label.trim(), value: s(a.value) ?? null, unit: s(a.unit) ?? null })),
      seo_title: s(v.seo_title), seo_description: s(v.seo_description), lifecycle_status: v.lifecycle_status,
    };
    Object.keys(body).forEach((k) => body[k] === undefined && delete body[k]);
    try {
      const p = await commerce.createProduct(body);
      toast.success("Product created", { description: "Approve it to publish it to your catalog API and feeds." });
      void qc.invalidateQueries({ queryKey: ["products"] });
      navigate(`/app/catalog/${p.id}`);
    } catch (e) {
      setError(e);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  });

  const reg = form.register;
  return (
    <>
      <Link to="/app/catalog" className="mb-4 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Catalog
      </Link>
      <PageHeader title="New product" description="Only the name is required. Fields you leave empty stay empty — AgentReady never fills them in for you." />
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        <ErrorNote error={error} />
        <Card>
          <CardHeader><CardTitle>Basic information</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <Field label="Product name *" htmlFor="c-name" error={errors.name?.message}><Input id="c-name" {...reg("name")} autoFocus /></Field>
            <Field label="Description" htmlFor="c-desc"><Textarea id="c-desc" rows={4} {...reg("description")} /></Field>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Brand" htmlFor="c-brand"><Input id="c-brand" {...reg("brand")} /></Field>
              <Field label="Manufacturer" htmlFor="c-mfr"><Input id="c-mfr" {...reg("manufacturer")} /></Field>
              <Field label="Product type" htmlFor="c-type" hint="e.g. physical, digital, service"><Input id="c-type" list="ptypes" {...reg("product_type")} /></Field>
              <datalist id="ptypes"><option value="physical" /><option value="digital" /><option value="service" /><option value="bundle" /></datalist>
              <Field label="SKU" htmlFor="c-sku"><Input id="c-sku" {...reg("sku")} /></Field>
              <Field label="GTIN / barcode" htmlFor="c-gtin" error={errors.gtin?.message}><Input id="c-gtin" inputMode="numeric" {...reg("gtin")} /></Field>
              <Field label="MPN" htmlFor="c-mpn"><Input id="c-mpn" {...reg("mpn")} /></Field>
              <Field label="Category" htmlFor="c-cat" hint={<Link className="underline" to="/app/categories">Manage categories</Link>}>
                <NativeSelect id="c-cat" className="w-full" {...reg("category_id")}>
                  <option value="">No category</option>
                  {categories.data?.items.map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Material" htmlFor="c-mat"><Input id="c-mat" {...reg("material")} /></Field>
              <Field label="Status" htmlFor="c-life">
                <NativeSelect id="c-life" className="w-full" {...reg("lifecycle_status")}>
                  <option value="draft">Draft</option>
                  <option value="active">Active</option>
                </NativeSelect>
              </Field>
            </div>
            <Field label="Product URL" htmlFor="c-url" error={errors.product_url?.message}><Input id="c-url" placeholder="https://" {...reg("product_url")} /></Field>
          </CardContent>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader><div><CardTitle>Pricing</CardTitle><CardDescription>Cost price is private and never appears in feeds or the catalog API.</CardDescription></div></CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <Field label="Regular price" htmlFor="c-price" error={errors.regular_price?.message}><Input id="c-price" inputMode="decimal" {...reg("regular_price")} /></Field>
              <Field label="Currency" htmlFor="c-cur" error={errors.currency?.message}><Input id="c-cur" maxLength={3} className="uppercase" {...reg("currency")} /></Field>
              <Field label="Sale price" htmlFor="c-sale" error={errors.sale_price?.message}><Input id="c-sale" inputMode="decimal" {...reg("sale_price")} /></Field>
              <Field label="Cost price" htmlFor="c-cost" error={errors.cost_price?.message}><Input id="c-cost" inputMode="decimal" {...reg("cost_price")} /></Field>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><div><CardTitle>Inventory</CardTitle><CardDescription>When tracked, availability is computed from stock.</CardDescription></div></CardHeader>
            <CardContent className="space-y-4">
              <label className="flex items-center gap-2 text-[13px]">
                <Checkbox checked={track} onCheckedChange={(v) => form.setValue("track_inventory", !!v)} aria-label="Track inventory" /> Track inventory
              </label>
              {track ? (
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="Stock on hand" htmlFor="c-qty" error={errors.stock_quantity?.message}><Input id="c-qty" inputMode="numeric" {...reg("stock_quantity")} /></Field>
                  <Field label="Low-stock at" htmlFor="c-low" error={errors.low_stock_threshold?.message} hint="Blank = default"><Input id="c-low" inputMode="numeric" {...reg("low_stock_threshold")} /></Field>
                  <Field label="Backorders" htmlFor="c-bo">
                    <NativeSelect id="c-bo" className="w-full" {...reg("backorder_policy")}><option value="deny">Not allowed</option><option value="allow">Allowed</option></NativeSelect>
                  </Field>
                </div>
              ) : (
                <Field label="Availability" htmlFor="c-av">
                  <NativeSelect id="c-av" className="w-full" {...reg("availability")}>
                    <option value="">Not stated</option>
                    {AVAILABILITY.map((a) => <option key={a} value={a}>{a.replace(/([a-z])([A-Z])/g, "$1 $2")}</option>)}
                  </NativeSelect>
                </Field>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader><CardTitle>Shipping, weight & dimensions</CardTitle></CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <Field label="Weight" htmlFor="c-w"><div className="flex gap-2"><Input id="c-w" inputMode="decimal" {...reg("weight")} /><Input className="w-20" aria-label="Weight unit" {...reg("weight_unit")} /></div></Field>
              <Field label="Dimensions" htmlFor="c-d" hint="e.g. 30 x 20 x 10"><div className="flex gap-2"><Input id="c-d" {...reg("dimensions")} /><Input className="w-20" aria-label="Dimension unit" {...reg("dimensions_unit")} /></div></Field>
              <Field label="Shipping class" htmlFor="c-sc"><Input id="c-sc" {...reg("shipping_class")} /></Field>
              <Field label="Dispatch time (days)" htmlFor="c-lt" error={errors.lead_time_days?.message}><Input id="c-lt" inputMode="numeric" {...reg("lead_time_days")} /></Field>
              <label className="flex items-center gap-2 text-[13px] sm:col-span-2">
                <Checkbox checked={form.watch("requires_shipping")} onCheckedChange={(v) => form.setValue("requires_shipping", !!v)} aria-label="Requires shipping" /> Requires shipping
              </label>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>SEO</CardTitle></CardHeader>
            <CardContent className="space-y-4">
              <Field label="SEO title" htmlFor="c-st"><Input id="c-st" {...reg("seo_title")} /></Field>
              <Field label="Meta description" htmlFor="c-sd"><Textarea id="c-sd" rows={3} {...reg("seo_description")} /></Field>
            </CardContent>
          </Card>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Images</CardTitle>
              <Button type="button" size="xs" variant="outline" onClick={() => images.append({ url: "", alt: "" })}><Plus /> Add image URL</Button>
            </CardHeader>
            <CardContent className="space-y-2">
              {images.fields.length === 0 && <p className="text-[13px] text-muted-foreground">No images.</p>}
              {images.fields.map((f, i) => (
                <div key={f.id} className="grid grid-cols-[1fr_140px_auto] gap-2">
                  <Input placeholder="https://…/image.jpg" aria-label="Image URL" {...reg(`images.${i}.url`)} aria-invalid={!!errors.images?.[i]?.url} />
                  <Input placeholder="Alt text" aria-label="Alt text" {...reg(`images.${i}.alt`)} />
                  <Button type="button" variant="ghost" size="icon" onClick={() => images.remove(i)} aria-label="Remove image"><X /></Button>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Specifications</CardTitle>
              <Button type="button" size="xs" variant="outline" onClick={() => attrs.append({ label: "", value: "", unit: "" })}><Plus /> Add</Button>
            </CardHeader>
            <CardContent className="space-y-2">
              {attrs.fields.length === 0 && <p className="text-[13px] text-muted-foreground">No specifications.</p>}
              {attrs.fields.map((f, i) => (
                <div key={f.id} className="grid grid-cols-[1fr_1fr_80px_auto] gap-2">
                  <Input placeholder="Name" aria-label="Specification name" {...reg(`attributes.${i}.label`)} aria-invalid={!!errors.attributes?.[i]?.label} />
                  <Input placeholder="Value" aria-label="Specification value" {...reg(`attributes.${i}.value`)} />
                  <Input placeholder="Unit" aria-label="Unit" {...reg(`attributes.${i}.unit`)} />
                  <Button type="button" variant="ghost" size="icon" onClick={() => attrs.remove(i)} aria-label="Remove specification"><X /></Button>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>

        <div className="flex justify-end gap-2">
          <Label className="sr-only">Actions</Label>
          <Button type="button" variant="outline" onClick={() => navigate("/app/catalog")}>Cancel</Button>
          <Button type="submit" loading={isSubmitting}>Create product</Button>
        </div>
      </form>
    </>
  );
}
