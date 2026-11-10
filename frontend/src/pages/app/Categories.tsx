import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, FolderTree, Pencil, Plus, RotateCcw, Tags, Trash2, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader } from "@/components/app/common";
import { ConfirmDialog } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, NativeSelect, Skeleton, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { commerce } from "@/services/commerce";
import type { AttributeDefinition, Category } from "@/types/commerce";

function depth(c: Category) {
  return c.path.split(" / ").length - 1;
}

function CategoryDialog({ open, onOpenChange, category, categories, onSaved }: {
  open: boolean; onOpenChange: (o: boolean) => void; category: Category | null; categories: Category[]; onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [desc, setDesc] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const init = () => {
    setName(category?.name ?? "");
    setParent(category?.parent_id ?? "");
    setDesc(category?.description ?? "");
    setErr(null);
  };
  const invalidParents = new Set(category ? categories.filter((c) => c.id === category.id || c.path.startsWith(`${category.path} / `)).map((c) => c.id) : []);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onOpenAutoFocus={init}>
        <DialogHeader><DialogTitle>{category ? "Edit category" : "New category"}</DialogTitle>
          <DialogDescription>Renaming or moving a category relabels all of its products.</DialogDescription></DialogHeader>
        <ErrorNote error={err} />
        <Field label="Name" htmlFor="cat-name"><Input id="cat-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Parent category" htmlFor="cat-parent">
          <NativeSelect id="cat-parent" className="w-full" value={parent} onChange={(e) => setParent(e.target.value)}>
            <option value="">None (top level)</option>
            {categories.filter((c) => !invalidParents.has(c.id) && !c.archived_at).map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
          </NativeSelect>
        </Field>
        <Field label="Description" htmlFor="cat-desc"><Textarea id="cat-desc" rows={2} value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button loading={busy} disabled={!name.trim()} onClick={async () => {
            setBusy(true);
            try {
              const body = { name: name.trim(), parent_id: parent || null, description: desc || undefined };
              if (category) await commerce.updateCategory(category.id, body);
              else await commerce.createCategory(body);
              toast.success(category ? "Category updated" : "Category created");
              onSaved();
              onOpenChange(false);
            } catch (e) {
              setErr(e);
            } finally {
              setBusy(false);
            }
          }}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AttributeDialog({ open, onOpenChange, def, categories, onSaved }: {
  open: boolean; onOpenChange: (o: boolean) => void; def: AttributeDefinition | null; categories: Category[]; onSaved: () => void;
}) {
  const [f, setF] = useState<Record<string, string | boolean>>({});
  const [err, setErr] = useState<unknown>(null);
  const init = () => {
    setF(def ? { name: def.name, data_type: def.data_type, unit: def.unit ?? "", description: def.description ?? "", is_required: def.is_required,
      allowed_values: def.allowed_values.join(", "), aliases: def.aliases.join(", "), category_id: def.category_id ?? "" }
      : { name: "", data_type: "string", unit: "", description: "", is_required: false, allowed_values: "", aliases: "", category_id: "" });
    setErr(null);
  };
  const s = (k: string) => (f[k] as string) ?? "";
  const list = (k: string) => s(k).split(",").map((x) => x.trim()).filter(Boolean);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onOpenAutoFocus={init}>
        <DialogHeader><DialogTitle>{def ? `Edit attribute: ${def.name}` : "New attribute"}</DialogTitle>
          <DialogDescription>Definitions add validation (required, type, allowed values) and map equivalent names to one canonical key.</DialogDescription></DialogHeader>
        <ErrorNote error={err} />
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name" htmlFor="ad-name"><Input id="ad-name" value={s("name")} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label="Type" htmlFor="ad-type">
            <NativeSelect id="ad-type" className="w-full" value={s("data_type")} onChange={(e) => setF({ ...f, data_type: e.target.value })}>
              <option value="string">Text</option><option value="number">Number</option><option value="boolean">Yes / no</option>
              <option value="date">Date</option><option value="enum">Controlled list</option>
            </NativeSelect>
          </Field>
          <Field label="Unit" htmlFor="ad-unit"><Input id="ad-unit" value={s("unit")} onChange={(e) => setF({ ...f, unit: e.target.value })} /></Field>
          <Field label="Category" htmlFor="ad-cat" hint="Applies to this category and its subcategories">
            <NativeSelect id="ad-cat" className="w-full" value={s("category_id")} onChange={(e) => setF({ ...f, category_id: e.target.value })}>
              <option value="">All products</option>
              {categories.map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
            </NativeSelect>
          </Field>
          {s("data_type") === "enum" && (
            <Field label="Allowed values" htmlFor="ad-vals" hint="Comma-separated" className="sm:col-span-2"><Input id="ad-vals" value={s("allowed_values")} onChange={(e) => setF({ ...f, allowed_values: e.target.value })} /></Field>
          )}
          <Field label="Also known as" htmlFor="ad-alias" hint="Comma-separated names that mean the same (e.g. memory, ram size)" className="sm:col-span-2">
            <Input id="ad-alias" value={s("aliases")} onChange={(e) => setF({ ...f, aliases: e.target.value })} />
          </Field>
          <Field label="Description" htmlFor="ad-desc" className="sm:col-span-2"><Input id="ad-desc" value={s("description")} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
          <label className="flex items-center gap-2 text-[13px]"><Checkbox checked={!!f.is_required} onCheckedChange={(v) => setF({ ...f, is_required: !!v })} aria-label="Required" /> Required</label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!s("name").trim()} onClick={async () => {
            const body = { name: s("name").trim(), data_type: s("data_type"), unit: s("unit") || null, description: s("description") || null,
              is_required: !!f.is_required, allowed_values: list("allowed_values"), aliases: list("aliases"), category_id: s("category_id") || null };
            try {
              if (def) await commerce.updateDefinition(def.id, body);
              else await commerce.createDefinition(body);
              toast.success("Attribute saved", { description: "Products were re-validated against it." });
              onSaved();
              onOpenChange(false);
            } catch (e) {
              setErr(e);
            }
          }}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function Categories() {
  const qc = useQueryClient();
  const [showArchived, setShowArchived] = useState(false);
  const cats = useQuery({ queryKey: ["categories-managed", showArchived], queryFn: () => commerce.categories(showArchived) });
  const defs = useQuery({ queryKey: ["attribute-definitions"], queryFn: commerce.definitions });
  const [catDialog, setCatDialog] = useState<{ open: boolean; c: Category | null }>({ open: false, c: null });
  const [defDialog, setDefDialog] = useState<{ open: boolean; d: AttributeDefinition | null }>({ open: false, d: null });
  const [del, setDel] = useState<Category | null>(null);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["categories-managed"] });
    void qc.invalidateQueries({ queryKey: ["attribute-definitions"] });
    void qc.invalidateQueries({ queryKey: ["products"] });
  };
  const items = useMemo(() => cats.data?.items ?? [], [cats.data]);
  const live = items.filter((c) => !c.archived_at);
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try {
      await fn();
      toast.success(msg);
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <>
      <PageHeader title="Categories & attributes" description="Organise products in a nested taxonomy and define the attributes each category should have." />
      <Tabs defaultValue="categories">
        <TabsList>
          <TabsTrigger value="categories"><FolderTree className="mr-1 h-3.5 w-3.5" /> Categories</TabsTrigger>
          <TabsTrigger value="attributes"><Tags className="mr-1 h-3.5 w-3.5" /> Attributes</TabsTrigger>
        </TabsList>
        <TabsContent value="categories">
          <div className="grid gap-4 lg:grid-cols-[1.5fr_1fr]">
            <Card className="overflow-hidden">
              <CardHeader className="flex-row items-center justify-between gap-2">
                <div><CardTitle>Category tree</CardTitle><CardDescription>{cats.data?.uncategorised ?? 0} product(s) have no managed category.</CardDescription></div>
                <div className="flex items-center gap-2">
                  <label className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground"><Checkbox checked={showArchived} onCheckedChange={(v) => setShowArchived(!!v)} aria-label="Show archived" /> Archived</label>
                  <Button size="sm" onClick={() => setCatDialog({ open: true, c: null })}><Plus /> New category</Button>
                </div>
              </CardHeader>
              <ErrorNote error={cats.error} />
              {cats.isLoading ? <div className="p-4"><Skeleton className="h-24" /></div> : items.length === 0 ? (
                <div className="p-6"><EmptyState icon={<FolderTree />} title="No categories yet" description="Create one, or build them from the category names found on your products." /></div>
              ) : (
                <Table>
                  <THead><TR><TH>Category</TH><TH className="text-right">Products</TH><TH className="w-32" /></TR></THead>
                  <TBody>
                    {items.map((c) => (
                      <TR key={c.id} className={c.archived_at ? "opacity-60" : ""}>
                        <TD>
                          <div style={{ paddingLeft: depth(c) * 18 }} className="flex items-center gap-1.5">
                            {depth(c) > 0 && <span className="text-muted-foreground">└</span>}
                            <Link to={`/app/catalog?category_id=${c.id}`} className="font-medium hover:underline">{c.name}</Link>
                            {c.archived_at && <Badge tone="warning">Archived</Badge>}
                          </div>
                        </TD>
                        <TD className="tabular text-right">{c.product_count}</TD>
                        <TD>
                          <div className="flex justify-end gap-1">
                            <Button size="xs" variant="ghost" aria-label={`Edit ${c.name}`} onClick={() => setCatDialog({ open: true, c })}><Pencil /></Button>
                            {c.archived_at ? (
                              <Button size="xs" variant="ghost" aria-label={`Restore ${c.name}`} onClick={() => act(() => commerce.updateCategory(c.id, { archived: false }), "Category restored")}><RotateCcw /></Button>
                            ) : (
                              <Button size="xs" variant="ghost" aria-label={`Archive ${c.name}`} onClick={() => act(() => commerce.updateCategory(c.id, { archived: true }), "Category archived")}><Archive /></Button>
                            )}
                            <Button size="xs" variant="ghost" aria-label={`Delete ${c.name}`} onClick={() => setDel(c)}><Trash2 /></Button>
                          </div>
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              )}
            </Card>
            <Card>
              <CardHeader><div><CardTitle>Category names found on products</CardTitle><CardDescription>Extracted or imported text that is not linked to a managed category yet.</CardDescription></div></CardHeader>
              <CardContent className="space-y-3">
                {!cats.data?.unmapped_texts.length ? <p className="text-[13px] text-muted-foreground">Everything is linked.</p> : (
                  <>
                    <ul className="space-y-1 text-[13px]">
                      {cats.data.unmapped_texts.slice(0, 12).map((u) => (
                        <li key={u.category} className="flex justify-between gap-2"><span className="truncate">{u.category}</span><span className="tabular text-muted-foreground">{u.count}</span></li>
                      ))}
                    </ul>
                    <Button size="sm" variant="outline" onClick={() => act(async () => {
                      const r = await commerce.importCategories();
                      toast.message(`${r.created} categories created, ${r.linked} products linked`);
                    }, "Categories created from product data")}><Wand2 /> Create categories from these</Button>
                  </>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>
        <TabsContent value="attributes">
          <Card className="overflow-hidden">
            <CardHeader className="flex-row items-center justify-between gap-2">
              <div><CardTitle>Attribute definitions</CardTitle><CardDescription>Values are checked during validation; missing required attributes appear in the Validation Center.</CardDescription></div>
              <Button size="sm" onClick={() => setDefDialog({ open: true, d: null })}><Plus /> New attribute</Button>
            </CardHeader>
            {!defs.data?.items.length ? <CardContent><p className="text-[13px] text-muted-foreground">No attribute definitions yet.</p></CardContent> : (
              <div className="overflow-x-auto">
                <Table>
                  <THead><TR><TH>Attribute</TH><TH>Key</TH><TH>Type</TH><TH>Applies to</TH><TH>Required</TH><TH>Also known as</TH><TH className="text-right">Products with value</TH><TH /></TR></THead>
                  <TBody>
                    {defs.data.items.map((d) => (
                      <TR key={d.id}>
                        <TD className="font-medium">{d.name}{d.unit && <span className="ml-1 text-muted-foreground">({d.unit})</span>}</TD>
                        <TD className="font-mono text-[12px]">{d.key}</TD>
                        <TD>{d.data_type === "enum" ? `List: ${d.allowed_values.join(", ")}` : d.data_type}</TD>
                        <TD className="text-muted-foreground">{live.find((c) => c.id === d.category_id)?.path ?? "All products"}</TD>
                        <TD>{d.is_required ? <Badge tone="warning">Required</Badge> : "—"}</TD>
                        <TD className="text-[12px] text-muted-foreground">{d.aliases.join(", ") || "—"}</TD>
                        <TD className="tabular text-right">{d.products_with_value ?? 0}</TD>
                        <TD><Button size="xs" variant="ghost" aria-label={`Edit ${d.name}`} onClick={() => setDefDialog({ open: true, d })}><Pencil /></Button></TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
            {!!defs.data?.observed_keys.length && (
              <CardContent className="border-t pt-4">
                <p className="mb-2 text-[12px] font-medium text-muted-foreground">Attributes found on your products</p>
                <div className="flex flex-wrap gap-1.5">{defs.data.observed_keys.slice(0, 40).map((k) => <Badge key={k.key}>{k.key} · {k.count}</Badge>)}</div>
              </CardContent>
            )}
          </Card>
        </TabsContent>
      </Tabs>
      <CategoryDialog open={catDialog.open} onOpenChange={(o) => setCatDialog({ ...catDialog, open: o })} category={catDialog.c} categories={live} onSaved={refresh} />
      <AttributeDialog open={defDialog.open} onOpenChange={(o) => setDefDialog({ ...defDialog, open: o })} def={defDialog.d} categories={live} onSaved={refresh} />
      <ConfirmDialog open={!!del} onOpenChange={(o) => !o && setDel(null)} title={`Delete "${del?.path}"?`} destructive confirmLabel="Delete"
        description="Only empty categories can be deleted. Archive it instead if products still use it."
        onConfirm={async () => { await commerce.deleteCategory(del!.id); toast.success("Category deleted"); refresh(); }} />
    </>
  );
}
