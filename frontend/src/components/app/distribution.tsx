import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff } from "lucide-react";
import { toast } from "sonner";
import { ErrorNote } from "@/components/app/common";
import { money } from "@/components/app/commerce";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Checkbox, Field, Input, Skeleton, Textarea } from "@/components/ui/primitives";
import { distribution } from "@/services/distribution";
import type { Channel, DistributionStatus, PublicProduct } from "@/types/distribution";
import { humanize } from "@/utils/format";

export const CHANNEL_LABELS: Record<Channel, string> = { agent_api: "Agent API", mcp: "MCP", feeds: "Feeds", webhooks: "Webhooks" };
export const AVAIL_TONE: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = {
  available: "success", out_of_stock: "danger", preorder: "info", backorder: "warning", discontinued: "neutral", unknown: "neutral",
};

export function DistStatusBadge({ status }: { status: DistributionStatus }) {
  if (status.distributed) return <Badge tone="success" dot>Distributed</Badge>;
  if (status.eligible) return <Badge tone="warning" dot>Eligible — channel off</Badge>;
  return <Badge tone="neutral" dot>Not distributed</Badge>;
}

export function AvailabilityBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <Badge>Withheld</Badge>;
  return <Badge tone={AVAIL_TONE[status] ?? "neutral"}>{humanize(status)}</Badge>;
}

/** Renders the exact JSON an external agent receives (from the public serializer), plus a readable summary. */
export function PublicPreview({ p }: { p: PublicProduct }) {
  const [raw, setRaw] = useState(false);
  return (
    <div className="space-y-3 text-[13px]" data-testid="public-preview">
      <div className="flex flex-wrap items-start gap-3">
        {p.images[0] ? (
          <img src={p.images[0]} alt="" className="h-20 w-20 rounded border object-cover" onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
        ) : (
          <div className="grid h-20 w-20 place-items-center rounded border text-[11px] text-muted-foreground">No image</div>
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-[15px] font-semibold">{p.name}</p>
          <p className="text-muted-foreground">{p.brand ?? "Brand unknown"} · {p.merchant.name}</p>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium" data-testid="preview-price">
              {p.price ? money(p.price.current, p.price.currency) : p.withheld_fields.includes("price") ? "Price withheld" : "Price unknown"}
            </span>
            {p.price?.on_sale && <Badge tone="info">On sale (was {money(p.price.regular, p.price.currency)})</Badge>}
            <AvailabilityBadge status={p.availability?.status} />
          </div>
          {p.url && <a className="block truncate text-[12px] text-info hover:underline" href={p.url} target="_blank" rel="noreferrer">{p.url}</a>}
        </div>
      </div>
      {p.description && <p className="line-clamp-4 whitespace-pre-line text-muted-foreground">{p.description}</p>}
      {p.attributes.length > 0 && (
        <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
          {p.attributes.map((a) => (
            <div key={a.key} className="flex justify-between gap-2 border-b border-dashed py-0.5">
              <span className="text-muted-foreground">{a.name}</span><span className="text-right">{a.value}{a.unit ? ` ${a.unit}` : ""}</span>
            </div>
          ))}
        </div>
      )}
      {p.variants.length > 0 && (
        <div>
          <p className="mb-1 font-medium">Variants ({p.variants.length})</p>
          <div className="flex flex-wrap gap-1.5">
            {p.variants.map((v) => (
              <Badge key={v.id} tone="neutral">{Object.values(v.options).join(" / ") || v.name}{v.price ? ` · ${money(v.price.current, v.price.currency)}` : ""}</Badge>
            ))}
          </div>
        </div>
      )}
      <div className="flex flex-wrap gap-1.5 text-[12px]">
        {p.unknown_fields.length > 0 && <span className="text-muted-foreground">Unknown to agents: {p.unknown_fields.join(", ")}</span>}
        {p.withheld_fields.length > 0 && <span className="text-warning">Withheld by you: {p.withheld_fields.join(", ")}</span>}
      </div>
      <Button size="xs" variant="outline" onClick={() => setRaw(!raw)}>{raw ? "Hide" : "Show"} JSON agents receive</Button>
      {raw && <pre className="max-h-80 overflow-auto rounded bg-muted p-2 font-mono text-[11px]">{JSON.stringify(p, null, 2)}</pre>}
    </div>
  );
}

/** Product-level distribution controls + preview (product detail tab and the Public products page). */
export function ProductDistributionPanel({ productId }: { productId: string }) {
  const qc = useQueryClient();
  const preview = useQuery({ queryKey: ["dist-preview", productId], queryFn: () => distribution.preview(productId) });
  const [desc, setDesc] = useState("");
  const [images, setImages] = useState("");
  const [attrs, setAttrs] = useState("");
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["dist-preview", productId] });
    void qc.invalidateQueries({ queryKey: ["dist-products"] });
    void qc.invalidateQueries({ queryKey: ["dist-overview"] });
  };
  const c = preview.data?.controls;
  useEffect(() => {
    if (c) {
      setChannels(c.distribution_channels);
      setAttrs((c.public_attribute_keys ?? []).join(", "));
    }
  }, [preview.data?.representation.id, c?.distribution_channels?.join(), c?.public_attribute_keys?.join()]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async (b: Record<string, unknown>, msg: string) => {
    setBusy(true);
    try {
      await distribution.updateProduct(productId, b);
      toast.success(msg);
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (preview.isLoading) return <Skeleton className="h-48" />;
  if (preview.error) return <ErrorNote error={preview.error} />;
  const st = preview.data!.status;
  const enabled = c?.distribution_enabled;
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_1.1fr]">
      <Card>
        <CardHeader><div><CardTitle className="flex items-center gap-2">Distribution <DistStatusBadge status={st} /></CardTitle>
          <CardDescription>What external agents, feeds and webhooks may receive for this product.</CardDescription></div></CardHeader>
        <CardContent className="space-y-4 text-[13px]">
          {st.reasons.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5 text-muted-foreground" data-testid="dist-reasons">{st.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant={enabled === true ? "default" : "outline"} disabled={busy} onClick={() => save({ distribution_enabled: true }, "Distribution enabled for this product")}><Eye /> Distribute</Button>
            <Button size="sm" variant={enabled === false ? "default" : "outline"} disabled={busy} onClick={() => save({ distribution_enabled: false }, "Distribution disabled for this product")}><EyeOff /> Do not distribute</Button>
            <Button size="sm" variant={enabled == null ? "secondary" : "ghost"} disabled={busy} onClick={() => save({ inherit: true }, "Following your distribution settings")}>Follow store settings</Button>
          </div>
          <Field label="Channels">
            <div className="flex flex-wrap gap-3">
              <label className="flex items-center gap-1.5"><Checkbox checked={channels === null} onCheckedChange={(v) => setChannels(v ? null : ["agent_api", "mcp", "feeds", "webhooks"])} /> All enabled channels</label>
              {(Object.keys(CHANNEL_LABELS) as Channel[]).map((c) => (
                <label key={c} className="flex items-center gap-1.5">
                  <Checkbox disabled={channels === null} checked={channels === null || channels.includes(c)}
                    onCheckedChange={(v) => setChannels(v ? [...(channels ?? []), c] : (channels ?? []).filter((x) => x !== c))} /> {CHANNEL_LABELS[c]}
                </label>
              ))}
            </div>
          </Field>
          <Button size="xs" variant="outline" disabled={busy || (channels !== null && channels.length === 0)}
            onClick={() => save(channels === null ? { all_channels: true } : { distribution_channels: channels }, "Channels saved")}>Save channels</Button>
          <Field label="Public description" htmlFor="pd-desc" hint="Shown to agents instead of your catalog description. Your catalog description and its source evidence are not changed.">
            <Textarea id="pd-desc" rows={3} value={desc} placeholder={c?.public_description ? "A public description is set — type to replace it" : "Leave empty to use the catalog description"} onChange={(e) => setDesc(e.target.value)} />
          </Field>
          <div className="flex gap-2">
            <Button size="xs" disabled={busy || !desc.trim()} onClick={() => save({ public_description: desc }, "Public description saved").then(() => setDesc(""))}>Save description</Button>
            {c?.public_description && <Button size="xs" variant="ghost" disabled={busy} onClick={() => save({ clear_public_description: true }, "Using the catalog description")}>Use catalog description</Button>}
          </div>
          <Field label="Public images" htmlFor="pd-images" hint="One https:// URL per line. Leave empty to use the product images.">
            <Textarea id="pd-images" rows={2} value={images} onChange={(e) => setImages(e.target.value)} placeholder={c?.public_images ? "Custom public images are set" : "https://…"} />
          </Field>
          <div className="flex gap-2">
            <Button size="xs" disabled={busy || !images.trim()} onClick={() => save({ public_images: images.split(/\s+/).filter(Boolean) }, "Public images saved").then(() => setImages(""))}>Save images</Button>
            {c?.public_images && <Button size="xs" variant="ghost" disabled={busy} onClick={() => save({ clear_public_images: true }, "Using product images")}>Use product images</Button>}
          </div>
          <Field label="Public attributes" htmlFor="pd-attrs" hint="Comma-separated attribute keys to share for this product. Empty = your store-wide rule.">
            <Input id="pd-attrs" value={attrs} onChange={(e) => setAttrs(e.target.value)} placeholder="e.g. voltage, power" />
          </Field>
          <Button size="xs" variant="outline" disabled={busy} onClick={() => save(attrs.trim() ? { public_attribute_keys: attrs.split(",").map((s) => s.trim()).filter(Boolean) } : { clear_public_attribute_keys: true }, "Public attributes saved")}>Save attributes</Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><div><CardTitle>What an agent sees</CardTitle>
          <CardDescription>{preview.data!.visible_to_agents ? "Live: this is the response the agent API returns now." : "Preview only — agents cannot see this product yet."}</CardDescription></div></CardHeader>
        <CardContent><PublicPreview p={preview.data!.representation} /></CardContent>
      </Card>
    </div>
  );
}
