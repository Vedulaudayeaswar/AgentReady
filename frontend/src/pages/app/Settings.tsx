import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Copy, KeyRound, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { ErrorNote, PageHeader, Stat } from "@/components/app/common";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { useAuth } from "@/hooks/useAuth";
import { api, tokenStore } from "@/services/api";
import { timeAgo } from "@/utils/format";

export default function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" />
      <Tabs defaultValue="business">
        <TabsList>
          <TabsTrigger value="business">Business profile</TabsTrigger>
          <TabsTrigger value="websites">Websites</TabsTrigger>
          <TabsTrigger value="account">Account</TabsTrigger>
          <TabsTrigger value="security">Security & API</TabsTrigger>
          <TabsTrigger value="ai">AI usage</TabsTrigger>
        </TabsList>
        <TabsContent value="business"><BusinessProfile /></TabsContent>
        <TabsContent value="websites"><WebsiteSettings /></TabsContent>
        <TabsContent value="account"><Account /></TabsContent>
        <TabsContent value="security"><Security /></TabsContent>
        <TabsContent value="ai"><AIUsage /></TabsContent>
      </Tabs>
    </>
  );
}

const bizSchema = z.object({
  name: z.string().trim().min(1, "Required").max(200),
  contact_email: z.string().email("Invalid email").or(z.literal("")),
  default_currency: z.string().trim().refine((v) => v === "" || /^[A-Za-z]{3}$/.test(v), "3-letter ISO code"),
  country: z.string().trim().refine((v) => v === "" || /^[A-Za-z]{2}$/.test(v), "2-letter ISO code"),
});

function BusinessProfile() {
  const { user, refresh } = useAuth();
  const b = user!.business;
  const [error, setError] = useState<unknown>(null);
  const form = useForm<z.infer<typeof bizSchema>>({
    resolver: zodResolver(bizSchema),
    values: { name: b.name, contact_email: b.contact_email ?? "", default_currency: b.default_currency ?? "", country: b.country ?? "" },
  });
  const { errors, isSubmitting } = form.formState;
  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    try {
      await api.updateBusiness({ name: v.name, contact_email: v.contact_email || null, default_currency: v.default_currency || null, country: v.country || null });
      await refresh();
      toast.success("Business profile saved");
    } catch (e) {
      setError(e);
    }
  });
  return (
    <Card className="max-w-2xl">
      <CardHeader><div><CardTitle>Business profile</CardTitle><CardDescription>Used in exports and the published catalog.</CardDescription></div></CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <ErrorNote error={error} />
          <Field label="Business name" htmlFor="b-name" error={errors.name?.message}><Input id="b-name" {...form.register("name")} /></Field>
          <Field label="Contact email" htmlFor="b-email" error={errors.contact_email?.message}><Input id="b-email" type="email" {...form.register("contact_email")} /></Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Default currency"
              htmlFor="b-cur"
              error={errors.default_currency?.message}
              hint="Applied only when a page shows a price without any currency. Marked as inferred."
            >
              <Input id="b-cur" maxLength={3} className="uppercase" placeholder="INR" {...form.register("default_currency")} />
            </Field>
            <Field label="Country" htmlFor="b-country" error={errors.country?.message}><Input id="b-country" maxLength={2} className="uppercase" placeholder="IN" {...form.register("country")} /></Field>
          </div>
          <Button type="submit" loading={isSubmitting}>Save</Button>
        </form>
      </CardContent>
    </Card>
  );
}

function WebsiteSettings() {
  const websites = useQuery({ queryKey: ["websites"], queryFn: api.websites });
  return (
    <Card className="overflow-hidden">
      <CardHeader><div><CardTitle>Website crawl settings</CardTitle><CardDescription>Defaults used for each scan of a website.</CardDescription></div></CardHeader>
      {websites.data?.items.length ? (
        <Table>
          <THead><TR className="hover:bg-transparent"><TH>Website</TH><TH>Max pages</TH><TH>Depth</TH><TH>Sitemap</TH><TH>JS rendering</TH><TH>Last scan</TH></TR></THead>
          <TBody>
            {websites.data.items.map((w) => (
              <TR key={w.id}>
                <TD className="font-medium">{w.domain}</TD>
                <TD><InlineNumber value={w.max_pages} max={1000} onSave={(v) => api.updateWebsite(w.id, { max_pages: v })} /></TD>
                <TD><InlineNumber value={w.max_depth} max={10} onSave={(v) => api.updateWebsite(w.id, { max_depth: v })} /></TD>
                <TD>{w.use_sitemap ? "On" : "Off"}</TD>
                <TD>{w.render_js ? "On" : "Off"}</TD>
                <TD className="text-muted-foreground">{timeAgo(w.last_scan_at)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <CardContent><p className="text-[13px] text-muted-foreground">No websites connected.</p></CardContent>
      )}
    </Card>
  );
}

function InlineNumber({ value, max, onSave }: { value: number; max: number; onSave: (v: number) => Promise<unknown> }) {
  const [v, setV] = useState(String(value));
  return (
    <Input
      className="h-8 w-20"
      type="number"
      min={1}
      max={max}
      value={v}
      onChange={(e) => setV(e.target.value)}
      onBlur={async () => {
        const n = Number(v);
        if (n !== value && n >= 0 && n <= max) {
          await onSave(n);
          toast.success("Saved");
        }
      }}
    />
  );
}

function Account() {
  const { user, refresh } = useAuth();
  const [name, setName] = useState(user?.full_name ?? "");
  return (
    <Card className="max-w-2xl">
      <CardHeader><CardTitle>Account</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <Field label="Email" htmlFor="a-email"><Input id="a-email" value={user?.email ?? ""} disabled /></Field>
        <Field label="Full name" htmlFor="a-name"><Input id="a-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Button
          onClick={async () => {
            await api.updateMe({ full_name: name || null });
            await refresh();
            toast.success("Saved");
          }}
        >
          Save
        </Button>
      </CardContent>
    </Card>
  );
}

const pwSchema = z
  .object({
    current: z.string().min(1, "Required"),
    next: z.string().min(10, "At least 10 characters").regex(/[A-Za-z]/, "Include a letter").regex(/\d/, "Include a number"),
  })
  .refine((v) => v.current !== v.next, { message: "Choose a different password", path: ["next"] });

function Security() {
  const { user, refresh } = useAuth();
  const [error, setError] = useState<unknown>(null);
  const [key, setKey] = useState<string | null>(null);
  const form = useForm<z.infer<typeof pwSchema>>({ resolver: zodResolver(pwSchema) });
  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    try {
      const { access_token } = await api.changePassword(v.current, v.next);
      tokenStore.set(access_token);
      form.reset({ current: "", next: "" });
      toast.success("Password changed. Other sessions were signed out.");
    } catch (e) {
      setError(e);
    }
  });
  const prefix = user?.business.catalog_api_key_prefix;
  const apiBase = `${window.location.origin}/api/v1/catalog`;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader><div><CardTitle>Change password</CardTitle><CardDescription>Changing your password signs out all other sessions.</CardDescription></div></CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <ErrorNote error={error} />
            <Field label="Current password" htmlFor="pw-c" error={form.formState.errors.current?.message}><Input id="pw-c" type="password" autoComplete="current-password" {...form.register("current")} /></Field>
            <Field label="New password" htmlFor="pw-n" error={form.formState.errors.next?.message}><Input id="pw-n" type="password" autoComplete="new-password" {...form.register("next")} /></Field>
            <Button type="submit" loading={form.formState.isSubmitting}>Update password</Button>
          </form>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><div><CardTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4" />Catalog API key</CardTitle><CardDescription>Read-only access to approved products for integrations.</CardDescription></div></CardHeader>
        <CardContent className="space-y-3 text-[13px]">
          <p>Current key: {prefix ? <span className="font-mono">{prefix}…</span> : <span className="text-muted-foreground">none</span>}</p>
          {key && (
            <div className="space-y-2 rounded-md border border-warning/30 bg-warning/5 p-3">
              <p className="flex items-center gap-1.5 font-medium"><AlertTriangle className="h-4 w-4 text-warning" />Copy this key now — it will not be shown again.</p>
              <div className="flex gap-2">
                <Input readOnly value={key} className="font-mono text-[12px]" />
                <Button variant="outline" size="icon" onClick={() => navigator.clipboard.writeText(key).then(() => toast.success("Copied"))} aria-label="Copy key"><Copy /></Button>
              </div>
            </div>
          )}
          <Button
            variant="outline"
            onClick={async () => {
              if (prefix && !window.confirm("Rotating the key immediately revokes the current one. Continue?")) return;
              const r = await api.rotateCatalogKey();
              setKey(r.catalog_api_key);
              await refresh();
            }}
          >
            <RefreshCw /> {prefix ? "Rotate key" : "Create key"}
          </Button>
          <pre className="overflow-x-auto rounded-md border bg-muted/30 p-3 font-mono text-[11.5px]">{`curl -H "X-Catalog-Key: <key>" \\\n  ${apiBase}`}</pre>
        </CardContent>
      </Card>
    </div>
  );
}

function AIUsage() {
  const { data, error } = useQuery({ queryKey: ["ai-settings"], queryFn: api.aiSettings });
  const [verifying, setVerifying] = useState(false);
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;
  const totals = data.usage.reduce(
    (a, u) => ({ req: a.req + u.requests, tok: a.tok + u.prompt_tokens + u.completion_tokens, cost: a.cost + u.estimated_cost_usd, cached: a.cached + u.cached }),
    { req: 0, tok: 0, cost: 0, cached: 0 },
  );
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>AI provider</CardTitle>
            <CardDescription>Configured on the server. API keys are never sent to the browser.</CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            loading={verifying}
            onClick={async () => {
              setVerifying(true);
              try {
                const r = await api.verifyAI();
                if (r.ok) toast.success(`Model ${r.model} is available`);
                else toast.error(r.message ?? "Model unavailable");
              } finally {
                setVerifying(false);
              }
            }}
          >
            Verify model
          </Button>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3 text-[13px]">
          <Badge tone={data.configured ? "success" : "warning"} dot>{data.configured ? "Configured" : "Not configured"}</Badge>
          <span>Provider: <span className="font-mono">{data.provider}</span></span>
          <span>Model: <span className="font-mono">{data.model ?? "—"}</span></span>
          <span>Mode: {data.extraction_mode}</span>
          <span>Budget: {data.max_ai_calls_per_scan} calls / scan</span>
          {data.is_mock && <Badge tone="warning">Mock provider — results are not from a real model</Badge>}
          {!data.configured && <p className="w-full text-muted-foreground">Without a GROQ_API_KEY, scans use deterministic extraction only.</p>}
        </CardContent>
      </Card>
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="AI requests" value={totals.req} hint={`${totals.cached} served from cache`} />
        <Stat label="Tokens used" value={totals.tok.toLocaleString()} />
        <Stat label="Estimated cost" value={`$${totals.cost.toFixed(4)}`} hint={`at $${data.pricing_usd_per_mtok.input}/$${data.pricing_usd_per_mtok.output} per 1M tokens`} />
        <Stat label="Tokens / 100 products" value={data.tokens_per_100_products?.toLocaleString() ?? "—"} />
      </div>
      {data.usage.length > 0 && (
        <Card className="overflow-hidden">
          <Table>
            <THead><TR className="hover:bg-transparent"><TH>Model</TH><TH className="text-right">Requests</TH><TH className="text-right">Errors</TH><TH className="text-right">Prompt tokens</TH><TH className="text-right">Completion tokens</TH><TH className="text-right">Avg latency</TH><TH className="text-right">Est. cost</TH></TR></THead>
            <TBody>
              {data.usage.map((u) => (
                <TR key={u.model}>
                  <TD className="font-mono text-[12px]">{u.model}</TD>
                  <TD className="tabular text-right">{u.requests}</TD>
                  <TD className="tabular text-right">{u.errors}</TD>
                  <TD className="tabular text-right">{u.prompt_tokens.toLocaleString()}</TD>
                  <TD className="tabular text-right">{u.completion_tokens.toLocaleString()}</TD>
                  <TD className="tabular text-right">{u.avg_latency_ms !== null ? `${u.avg_latency_ms} ms` : "—"}</TD>
                  <TD className="tabular text-right">${u.estimated_cost_usd.toFixed(4)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Card>
      )}
      <p className="text-[12px] text-muted-foreground">Costs are estimates from configurable per-token prices; check your Groq console for billing.</p>
    </div>
  );
}
