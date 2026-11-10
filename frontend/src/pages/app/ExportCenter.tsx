import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Braces, Download, FileSpreadsheet, Layers, Quote } from "lucide-react";
import { toast } from "sonner";
import { EmptyState, ErrorNote, PageHeader } from "@/components/app/common";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, NativeSelect, Switch, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { api } from "@/services/api";
import type { ExportFormat, ExportJob } from "@/types/api";
import { bytes, formatDate } from "@/utils/format";
import { cn } from "@/utils/cn";

const FORMATS: { key: ExportFormat; name: string; icon: typeof Braces; body: string }[] = [
  { key: "json", name: "JSON", icon: Braces, body: "AgentReady product records with attributes, variants and status." },
  { key: "csv", name: "CSV", icon: FileSpreadsheet, body: "One row per product; attributes become attr:* columns." },
  { key: "jsonld", name: "JSON-LD", icon: Quote, body: "schema.org ItemList of Product + Offer." },
  { key: "feed", name: "Product feed", icon: Layers, body: "agentready-feed/1.0 — versioned format for integrations." },
];

export default function ExportCenter() {
  const qc = useQueryClient();
  const [format, setFormat] = useState<ExportFormat>("jsonld");
  const [includeUnapproved, setIncludeUnapproved] = useState(false);
  const [category, setCategory] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const exports = useQuery({ queryKey: ["exports"], queryFn: api.exports });
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const job = await api.createExport({ format, include_unapproved: includeUnapproved, category: category || undefined });
      await api.downloadExport(job);
      toast.success(`Exported ${job.product_count} products`);
      void qc.invalidateQueries({ queryKey: ["exports"] });
      void qc.invalidateQueries({ queryKey: ["overview"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const download = async (job: ExportJob) => {
    try {
      await api.downloadExport(job);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <>
      <PageHeader title="Export Center" description="Download your catalog in machine-readable formats." />
      <Card>
        <CardHeader>
          <div>
            <CardTitle>New export</CardTitle>
            <CardDescription>
              {overview.data ? `${overview.data.approval.approved} approved of ${overview.data.total_products} products.` : " "} Rejected products are never exported.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {FORMATS.map((f) => (
              <button
                key={f.key}
                type="button"
                onClick={() => setFormat(f.key)}
                className={cn("rounded-lg border p-4 text-left transition-colors hover:bg-accent/60", format === f.key && "border-foreground/40 bg-accent/60 ring-1 ring-foreground/10")}
              >
                <f.icon className="h-4 w-4 text-muted-foreground" />
                <p className="mt-2 text-[13px] font-semibold">{f.name}</p>
                <p className="mt-1 text-[12px] text-muted-foreground">{f.body}</p>
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-6">
            <label className="flex items-center gap-2 text-[13px]">
              <Switch checked={includeUnapproved} onCheckedChange={setIncludeUnapproved} />
              Include products pending review
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              Category
              <NativeSelect value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">All</option>
                {categories.data?.items.map((c) => <option key={c.category} value={c.category}>{c.category}</option>)}
              </NativeSelect>
            </label>
            <div className="flex-1" />
            <Button onClick={run} loading={busy}><Download /> Generate & download</Button>
          </div>
          {includeUnapproved && (
            <p className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[12.5px]">
              Pending products have not been reviewed. Each record includes its approval status so downstream systems can tell them apart.
            </p>
          )}
          <ErrorNote error={error} />
        </CardContent>
      </Card>

      <Card className="mt-6 overflow-hidden">
        <CardHeader><CardTitle>Export history</CardTitle></CardHeader>
        {exports.data?.items.length ? (
          <Table>
            <THead><TR className="hover:bg-transparent"><TH>Created</TH><TH>Format</TH><TH>Products</TH><TH>Scope</TH><TH>Size</TH><TH className="text-right">File</TH></TR></THead>
            <TBody>
              {exports.data.items.map((e) => (
                <TR key={e.id}>
                  <TD className="text-muted-foreground">{formatDate(e.created_at)}</TD>
                  <TD><Badge className="font-mono uppercase">{e.format}</Badge></TD>
                  <TD className="tabular">{e.product_count}</TD>
                  <TD className="text-muted-foreground">
                    {e.filters.include_unapproved ? "Approved + pending" : "Approved only"}
                    {Array.isArray(e.filters.product_ids) && ` · ${(e.filters.product_ids as string[]).length} selected`}
                    {typeof e.filters.category === "string" && e.filters.category ? ` · ${e.filters.category}` : ""}
                  </TD>
                  <TD className="tabular text-muted-foreground">{bytes(e.size_bytes)}</TD>
                  <TD className="text-right"><Button size="xs" variant="outline" onClick={() => download(e)}><Download /> {e.filename}</Button></TD>
                </TR>
              ))}
            </TBody>
          </Table>
        ) : (
          <CardContent><EmptyState icon={<Download />} title="No exports yet" description="Generated exports are kept here for download." /></CardContent>
        )}
      </Card>
    </>
  );
}
