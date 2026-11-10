import { useState } from "react";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Globe, Info, Play, RefreshCw, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { ErrorNote, PageHeader } from "@/components/app/common";
import { ScanProgressPanel } from "@/components/app/ScanProgressPanel";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, Switch, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { ApiError, api } from "@/services/api";
import { timeAgo } from "@/utils/format";

const schema = z.object({
  url: z
    .string()
    .trim()
    .min(3, "Enter your website URL")
    .refine((v) => /^(https?:\/\/)?[a-z0-9.-]+\.[a-z]{2,}(:\d+)?(\/.*)?$/i.test(v), "Enter a valid website address, e.g. https://example.com"),
  max_pages: z.coerce.number().int().min(1).max(1000),
  max_depth: z.coerce.number().int().min(0).max(10),
  use_sitemap: z.boolean(),
  render_js: z.boolean(),
});
type FormValues = z.infer<typeof schema>;

export default function ConnectWebsite() {
  const qc = useQueryClient();
  const [scanId, setScanId] = useState<string | null>(null);
  const [check, setCheck] = useState<Awaited<ReturnType<typeof api.validateUrl>> | null>(null);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const websites = useQuery({ queryKey: ["websites"], queryFn: api.websites });

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { url: "", max_pages: 50, max_depth: 3, use_sitemap: true, render_js: false },
  });
  const { errors } = form.formState;

  const validate = useMutation({
    mutationFn: (url: string) => api.validateUrl(url),
    onMutate: () => {
      setCheck(null);
      setCheckError(null);
    },
    onSuccess: setCheck,
    onError: setCheckError,
  });

  const startScan = async (websiteId: string) => {
    const scan = await api.startScan(websiteId);
    setScanId(scan.id);
    void qc.invalidateQueries({ queryKey: ["overview"] });
    toast.success("Scan started");
  };

  const onSubmit = form.handleSubmit(async (v) => {
    setSubmitError(null);
    try {
      let websiteId: string;
      try {
        websiteId = (await api.createWebsite(v)).id;
      } catch (e) {
        if (e instanceof ApiError && e.code === "website_exists") websiteId = String(e.details?.website_id);
        else throw e;
      }
      await qc.invalidateQueries({ queryKey: ["websites"] });
      await startScan(websiteId);
    } catch (e) {
      setSubmitError(e);
    }
  });

  return (
    <>
      <PageHeader title="Connect Website" description="Point AgentReady at a website you own or are authorised to catalog." />
      <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Website</CardTitle>
              <CardDescription>We'll validate the address and check it is reachable before scanning.</CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            <form onSubmit={onSubmit} className="space-y-5" noValidate>
              <ErrorNote error={submitError} />
              <Field label="Website URL" htmlFor="url" error={errors.url?.message}>
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Globe className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
                    <Input id="url" placeholder="https://yourstore.com" className="pl-8" autoFocus {...form.register("url")} aria-invalid={!!errors.url} />
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    loading={validate.isPending}
                    onClick={async () => {
                      if (await form.trigger("url")) validate.mutate(form.getValues("url"));
                    }}
                  >
                    Check URL
                  </Button>
                </div>
              </Field>
              {checkError ? <ErrorNote error={checkError} /> : null}
              {check && (
                <div className="space-y-1 rounded-md border bg-muted/30 px-3 py-2.5 text-[13px]">
                  <p className="flex items-center gap-2">
                    {check.reachable ? <CheckCircle2 className="h-4 w-4 text-success" /> : <XCircle className="h-4 w-4 text-destructive" />}
                    {check.reachable ? "Website reachable" : `Not reachable: ${check.error ?? `HTTP ${check.status_code}`}`}
                  </p>
                  <p className="pl-6 text-muted-foreground">Normalized: <span className="font-mono text-[12px]">{check.normalized_url}</span></p>
                  <p className="pl-6 text-muted-foreground">robots.txt: {check.robots_txt_found ? "found — rules will be respected" : "not found"}</p>
                </div>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Maximum pages" htmlFor="max_pages" error={errors.max_pages?.message} hint="The server may enforce a lower limit.">
                  <Input id="max_pages" type="number" min={1} max={1000} {...form.register("max_pages")} />
                </Field>
                <Field label="Crawl depth" htmlFor="max_depth" error={errors.max_depth?.message} hint="Links followed from the homepage.">
                  <Input id="max_depth" type="number" min={0} max={10} {...form.register("max_depth")} />
                </Field>
              </div>
              <div className="space-y-3 rounded-md border p-3">
                <Controller
                  control={form.control}
                  name="use_sitemap"
                  render={({ field }) => (
                    <label className="flex items-center justify-between gap-4 text-[13px]">
                      <span>
                        <span className="font-medium">Use sitemaps</span>
                        <span className="block text-muted-foreground">Discover pages from sitemap.xml and robots.txt sitemap entries.</span>
                      </span>
                      <Switch checked={field.value} onCheckedChange={field.onChange} />
                    </label>
                  )}
                />
                <Controller
                  control={form.control}
                  name="render_js"
                  render={({ field }) => (
                    <label className="flex items-center justify-between gap-4 text-[13px]">
                      <span>
                        <span className="font-medium">Render JavaScript pages</span>
                        <span className="block text-muted-foreground">Only used when enabled on the server (Playwright).</span>
                      </span>
                      <Switch checked={field.value} onCheckedChange={field.onChange} />
                    </label>
                  )}
                />
              </div>
              <Button type="submit" loading={form.formState.isSubmitting}><Play /> Start scan</Button>
            </form>
          </CardContent>
        </Card>

        <Card className="h-fit">
          <CardHeader><CardTitle className="flex items-center gap-2"><Info className="h-4 w-4 text-muted-foreground" />Crawl requirements</CardTitle></CardHeader>
          <CardContent>
            <ul className="space-y-2.5 text-[13px] text-muted-foreground">
              <li>• The site must be publicly reachable over http(s) on standard ports.</li>
              <li>• robots.txt rules are respected; disallowed pages are skipped.</li>
              <li>• Pages behind logins, paywalls or anti-bot challenges are not accessed.</li>
              <li>• Requests are rate-limited (at least one second apart by default).</li>
              <li>• Private, internal and cloud-metadata addresses are blocked.</li>
              <li>• Only scan websites you own or are authorised to catalog.</li>
            </ul>
          </CardContent>
        </Card>
      </div>

      {scanId && <div className="mt-6"><ScanProgressPanel scanId={scanId} /></div>}

      <Card className="mt-6">
        <CardHeader>
          <div>
            <CardTitle>Connected websites</CardTitle>
            <CardDescription>Rescan to detect new, changed and removed products.</CardDescription>
          </div>
        </CardHeader>
        {websites.data?.items.length ? (
          <Table>
            <THead><TR><TH>Website</TH><TH>Limits</TH><TH>Last scan</TH><TH className="text-right">Actions</TH></TR></THead>
            <TBody>
              {websites.data.items.map((w) => (
                <TR key={w.id}>
                  <TD>
                    <p className="font-medium">{w.domain}</p>
                    <p className="font-mono text-[11.5px] text-muted-foreground">{w.normalized_url}</p>
                  </TD>
                  <TD className="text-muted-foreground">{w.max_pages} pages · depth {w.max_depth} {w.use_sitemap && <Badge className="ml-1">sitemap</Badge>}</TD>
                  <TD className="text-muted-foreground">{timeAgo(w.last_scan_at)}</TD>
                  <TD className="text-right">
                    <div className="flex justify-end gap-1.5">
                      <Button size="xs" variant="outline" onClick={() => startScan(w.id).catch((e) => toast.error(e.message))}>
                        <RefreshCw /> Rescan
                      </Button>
                      <Button
                        size="xs"
                        variant="ghost"
                        aria-label="Remove website"
                        onClick={async () => {
                          await api.deleteWebsite(w.id);
                          void qc.invalidateQueries({ queryKey: ["websites"] });
                          toast("Website removed. Its products remain in your catalog.");
                        }}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        ) : (
          <CardContent><p className="text-[13px] text-muted-foreground">No websites connected yet.</p></CardContent>
        )}
      </Card>
    </>
  );
}
