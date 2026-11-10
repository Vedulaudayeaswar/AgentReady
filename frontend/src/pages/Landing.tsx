import { Link } from "react-router-dom";
import {
  ArrowRight,
  Boxes,
  Braces,
  CheckCircle2,
  FileSearch,
  FileSpreadsheet,
  Globe,
  Layers,
  ListChecks,
  Quote,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Table2,
  Wrench,
} from "lucide-react";
import { Logo, ThemeToggle } from "@/components/app/common";
import { Badge, Button } from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";

const STEPS = [
  { icon: Globe, title: "Connect your website", body: "Enter the URL you already have. We validate it and read robots.txt and sitemaps before crawling anything." },
  { icon: ScanSearch, title: "Discover product pages", body: "A polite, bounded crawler finds pages that look like products using markup, prices, identifiers and page structure." },
  { icon: FileSearch, title: "Extract with evidence", body: "Structured data and tables are parsed deterministically first. AI handles only what rules cannot — and every value keeps its source." },
  { icon: ListChecks, title: "Validate and review", body: "Missing prices, conflicting values and possible duplicates are flagged. You edit, resolve and approve." },
  { icon: Braces, title: "Export a machine-readable catalog", body: "Download JSON, CSV, schema.org JSON-LD or a versioned product feed, or read it from the catalog API." },
];

const USE_CASES = [
  { icon: Wrench, title: "Industrial & B2B suppliers", body: "Spec-heavy price lists in HTML tables become products with voltage, RPM and dimensions as typed attributes." },
  { icon: Table2, title: "Static & legacy websites", body: "Hand-built HTML sites with no e-commerce platform get a clean catalog without a rebuild." },
  { icon: Boxes, title: "Distributors & wholesalers", body: "Large, inconsistent listings normalized into one schema with duplicates surfaced for review." },
  { icon: Layers, title: "Agencies", body: "Produce structured product data for client sites as a first step toward AI-ready commerce." },
];

const FORMATS = [
  { icon: Braces, name: "JSON", body: "Structured AgentReady product records." },
  { icon: FileSpreadsheet, name: "CSV", body: "Spreadsheet-friendly, attributes flattened into columns." },
  { icon: Quote, name: "JSON-LD", body: "schema.org Product + Offer, ready to embed or share." },
  { icon: Layers, name: "Product feed", body: "Versioned, documented format for future integrations." },
];

const SAMPLE = `{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "12V DC Motor",
  "sku": "MTR001",
  "brand": { "@type": "Brand", "name": "VoltPro" },
  "additionalProperty": [
    { "@type": "PropertyValue", "name": "Voltage", "value": "12", "unitText": "V" },
    { "@type": "PropertyValue", "name": "Speed", "value": "3000", "unitText": "RPM" }
  ],
  "offers": {
    "@type": "Offer",
    "price": "450.00",
    "priceCurrency": "INR",
    "availability": "https://schema.org/InStock"
  }
}`;

export default function Landing() {
  const { user } = useAuth();
  const cta = user ? "/app" : "/signup";
  return (
    <div className="min-h-screen bg-background">
      {/* nav */}
      <header className="sticky top-0 z-30 border-b bg-background/85 backdrop-blur">
        <div className="container flex h-14 items-center gap-6">
          <Logo />
          <nav className="hidden items-center gap-5 text-[13px] text-muted-foreground md:flex">
            <a href="#how" className="hover:text-foreground">How it works</a>
            <a href="#use-cases" className="hover:text-foreground">Use cases</a>
            <a href="#formats" className="hover:text-foreground">Formats</a>
            <a href="#roadmap" className="hover:text-foreground">Roadmap</a>
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            {user ? (
              <Button asChild size="sm"><Link to="/app">Open dashboard</Link></Button>
            ) : (
              <>
                <Button asChild variant="ghost" size="sm"><Link to="/login">Sign in</Link></Button>
                <Button asChild size="sm"><Link to="/signup">Get started</Link></Button>
              </>
            )}
          </div>
        </div>
      </header>

      {/* hero */}
      <section className="border-b">
        <div className="container grid gap-12 py-20 lg:grid-cols-[1.1fr_1fr] lg:py-28">
          <div className="flex flex-col justify-center">
            <Badge tone="neutral" className="mb-5 w-fit">Phase 1 · AI Catalog Generator</Badge>
            <h1 className="text-4xl font-semibold leading-[1.1] tracking-tight sm:text-5xl">
              Make Your Products Ready for AI Commerce.
            </h1>
            <p className="mt-5 max-w-xl text-base leading-7 text-muted-foreground">
              Turn any existing product website into a structured, machine-readable catalog. No website rebuild required.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Button asChild size="lg">
                <Link to={cta}>Generate Your AI-Ready Catalog <ArrowRight /></Link>
              </Button>
              <Button asChild size="lg" variant="outline"><a href="#how">See how it works</a></Button>
            </div>
            <ul className="mt-8 grid gap-2 text-[13px] text-muted-foreground sm:grid-cols-2">
              {["Respects robots.txt", "Source evidence for every value", "Never invents prices", "You approve before publishing"].map((t) => (
                <li key={t} className="flex items-center gap-2"><CheckCircle2 className="h-4 w-4 text-success" />{t}</li>
              ))}
            </ul>
          </div>
          <div className="relative">
            <div className="rounded-xl border bg-card shadow-sm">
              <div className="flex items-center gap-1.5 border-b px-4 py-2.5">
                <span className="h-2.5 w-2.5 rounded-full bg-muted-foreground/25" />
                <span className="h-2.5 w-2.5 rounded-full bg-muted-foreground/25" />
                <span className="h-2.5 w-2.5 rounded-full bg-muted-foreground/25" />
                <span className="ml-3 font-mono text-[11.5px] text-muted-foreground">catalog.jsonld · example output</span>
              </div>
              <pre className="overflow-x-auto p-4 font-mono text-[12px] leading-5 text-foreground/90 scrollbar-thin">{SAMPLE}</pre>
            </div>
            <div className="mt-3 rounded-lg border bg-card px-4 py-3 text-[12.5px]">
              <div className="flex items-center justify-between">
                <span className="font-medium">price → 450.00 INR</span>
                <Badge tone="neutral">Deterministic</Badge>
              </div>
              <p className="mt-1 font-mono text-[11.5px] text-muted-foreground">source: "Rs. 450" · html_table · /price-list</p>
            </div>
          </div>
        </div>
      </section>

      {/* problem / solution */}
      <section className="border-b">
        <div className="container grid gap-10 py-20 md:grid-cols-2">
          <div>
            <p className="text-[12px] font-medium uppercase tracking-wider text-muted-foreground">The problem</p>
            <h2 className="mt-3 text-2xl font-semibold">Humans can read your website. Machines struggle.</h2>
            <p className="mt-4 text-muted-foreground">
              Many businesses list products in raw HTML, ad-hoc tables, inconsistent descriptions and legacy systems.
              AI agents and automated commerce systems need consistent, structured data to understand what you sell,
              what it costs and whether it is available.
            </p>
          </div>
          <div>
            <p className="text-[12px] font-medium uppercase tracking-wider text-muted-foreground">The solution</p>
            <h2 className="mt-3 text-2xl font-semibold">A validated catalog generated from what you already have.</h2>
            <p className="mt-4 text-muted-foreground">
              AgentReady crawls your existing site, extracts products, normalizes them into one schema and validates the
              result. Every field links back to the exact text it came from, so you can trust — and correct — what is published.
            </p>
          </div>
        </div>
      </section>

      {/* how */}
      <section id="how" className="border-b">
        <div className="container py-20">
          <h2 className="text-2xl font-semibold">How it works</h2>
          <p className="mt-2 text-muted-foreground">Deterministic first, AI only where it adds value, humans in control.</p>
          <ol className="mt-10 grid gap-px overflow-hidden rounded-lg border bg-border md:grid-cols-5">
            {STEPS.map((s, i) => (
              <li key={s.title} className="bg-card p-5">
                <div className="flex items-center gap-2 text-muted-foreground">
                  <span className="font-mono text-[11px]">0{i + 1}</span>
                  <s.icon className="h-4 w-4" />
                </div>
                <h3 className="mt-3 text-[14px] font-semibold">{s.title}</h3>
                <p className="mt-1.5 text-[13px] leading-5 text-muted-foreground">{s.body}</p>
              </li>
            ))}
          </ol>
          <div className="mt-6 grid gap-3 md:grid-cols-3">
            {[
              { icon: ShieldCheck, t: "Safe crawling", d: "Robots rules, rate limits and strict network protections. No login walls or anti-bot bypassing." },
              { icon: Sparkles, t: "Grounded AI", d: "AI output is checked against the page text; unsupported prices and availability are discarded." },
              { icon: ListChecks, t: "Change detection", d: "Rescan anytime. Review new, changed and removed products before anything is updated." },
            ].map((f) => (
              <div key={f.t} className="flex gap-3 rounded-lg border p-4">
                <f.icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <div>
                  <p className="text-[13px] font-medium">{f.t}</p>
                  <p className="mt-0.5 text-[13px] text-muted-foreground">{f.d}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* use cases */}
      <section id="use-cases" className="border-b">
        <div className="container py-20">
          <h2 className="text-2xl font-semibold">Built for businesses with real product data — and messy websites</h2>
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {USE_CASES.map((u) => (
              <div key={u.title} className="rounded-lg border p-5">
                <u.icon className="h-4 w-4 text-muted-foreground" />
                <h3 className="mt-3 text-[14px] font-semibold">{u.title}</h3>
                <p className="mt-1.5 text-[13px] leading-5 text-muted-foreground">{u.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* formats */}
      <section id="formats" className="border-b">
        <div className="container py-20">
          <h2 className="text-2xl font-semibold">Export formats</h2>
          <p className="mt-2 text-muted-foreground">Only approved products are exported by default.</p>
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {FORMATS.map((f) => (
              <div key={f.name} className="rounded-lg border bg-card p-5">
                <f.icon className="h-4 w-4 text-muted-foreground" />
                <h3 className="mt-3 font-mono text-[13px] font-semibold">{f.name}</h3>
                <p className="mt-1.5 text-[13px] text-muted-foreground">{f.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* roadmap */}
      <section id="roadmap" className="border-b">
        <div className="container py-20">
          <h2 className="text-2xl font-semibold">Where AgentReady is going</h2>
          <p className="mt-2 max-w-2xl text-muted-foreground">
            Today AgentReady generates and validates your catalog. The roadmap below is planned work, not available
            functionality, and no integrations or partnerships exist yet.
          </p>
          <div className="mt-10 grid gap-4 md:grid-cols-4">
            {[
              { p: "Phase 1", t: "AI Catalog Generator", d: "Crawl, extract, validate, review, export.", now: true },
              { p: "Phase 2", t: "Catalog management", d: "Inventory, pricing and automatic synchronization." },
              { p: "Phase 3", t: "AI shopping distribution", d: "Feeds and agent-readable APIs for supported AI shopping platforms." },
              { p: "Phase 4", t: "Commerce analytics", d: "Discovery and conversion insight across AI channels." },
            ].map((r) => (
              <div key={r.p} className="rounded-lg border p-5">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] text-muted-foreground">{r.p}</span>
                  {r.now ? <Badge tone="success" dot>Available</Badge> : <Badge tone="neutral">Planned</Badge>}
                </div>
                <h3 className="mt-3 text-[14px] font-semibold">{r.t}</h3>
                <p className="mt-1.5 text-[13px] text-muted-foreground">{r.d}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* cta */}
      <section className="border-b">
        <div className="container flex flex-col items-start justify-between gap-6 py-16 md:flex-row md:items-center">
          <div>
            <h2 className="text-2xl font-semibold">See your catalog the way machines will.</h2>
            <p className="mt-2 text-muted-foreground">Connect a website and review the results in minutes.</p>
          </div>
          <Button asChild size="lg">
            <Link to={cta}>Generate Your AI-Ready Catalog <ArrowRight /></Link>
          </Button>
        </div>
      </section>

      <footer>
        <div className="container flex flex-col gap-4 py-10 text-[13px] text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <Logo className="text-foreground" />
            <span>Make Every Product Ready for AI Commerce.</span>
          </div>
          <div className="flex gap-5">
            <Link to="/login" className="hover:text-foreground">Sign in</Link>
            <Link to="/signup" className="hover:text-foreground">Create account</Link>
            <a href="/docs" className="hover:text-foreground">API docs</a>
          </div>
        </div>
      </footer>
    </div>
  );
}
