# AgentReady merchant guide

AgentReady turns the products on your existing website into a clean, checked product catalog — and then lets AI
shopping assistants find those products, under your control. You do not need to rebuild your website.

This guide explains every part of the app in plain language: **what it does, why it exists, how to use it, what
happens behind the scenes, and how it connects to the rest.**

> **What is real and what is not.** Everything you see in AgentReady comes from your website, your edits, or
> things that actually happened (for example, an AI agent searching your catalog). AgentReady never invents sales,
> orders, revenue, visitors or conversion numbers. When information is missing, it says so.

---

## The big picture

```
Your website ─▶ Scan ─▶ Product data with evidence ─▶ You review & approve ─▶ Your catalog
                                                                              │
                     Pricing, stock, variants, categories (you manage) ◀──────┤
                                                                              ▼
                 You switch on distribution ─▶ AI agents, MCP clients, product feeds, webhooks
                                                                              │
                     What agents searched for ─▶ Insights, quality, recommendations, reports
```

---

## 1. Your account

**What it does.** Your account holds one business ("store"). Everything you create is private to your business —
other merchants can never see it.

**How to use it.** Sign up at `/signup` with your business name, email and a password of at least 10 characters.
**Settings** lets you change your business profile, password, and see AI usage. **Sign out** from the menu at the
bottom of the sidebar.

**Behind the scenes.** Passwords are stored as one-way hashes. Your session lasts 30 minutes and ends when you
close the browser tab. After 10 wrong passwords for your email in 15 minutes, sign-in pauses for that account to
protect it; resetting your password clears this.

---

## 2. Connect Website and scanning

**What it does.** You enter your shop's address; AgentReady visits the pages it is allowed to (it respects your
`robots.txt`), finds product pages and reads product information from them.

**Why it exists.** Most shops already describe their products on their website. Scanning saves you from typing
your catalog again.

**How to use it.** **Connect Website** → enter the URL (for example `https://yourshop.com`) → **Start scan**. You
see live progress: pages discovered, product pages found, products extracted. A scan usually takes a few minutes.
**Scan History** lists every scan; open one to see which pages were read, skipped or failed and why.

**Behind the scenes.** AgentReady first reads structured data that many shops already publish (schema.org
JSON-LD, microdata, specification tables). Only when that is not enough does it ask the **Groq AI model** to read
the page — and every value the AI returns is checked against the page text. Prices that do not appear on the page
are thrown away. The crawler cannot be pointed at private/internal addresses.

**Connects to.** Products land in the **Product Catalog** with their **evidence**. Later scans (manual or
scheduled in **Synchronization**) detect changes on your website.

---

## 3. Product Catalog, evidence and approval

**What it does.** Lists all your products with their quality status. Each product page shows its details,
**Source evidence** (where each value came from: page, text fragment, method — "structured data", "table", "AI ·
model name" or "merchant"), validation issues, history, pricing, inventory and variants.

**Why it exists.** You stay in control: nothing is published until you approve it, and you can always see why
AgentReady believes a value.

**How to use it.**
* Tick products → **Approve** (or open a product → **Approve**). Approving an active product also publishes it.
* **Edit** any field. Your edits are marked as *merchant edits* and are **never overwritten by later scans** —
  if your website later shows something different, you get a *conflict* to decide on.
* **New product** creates a product by hand. **Archive / Delete** keep history and can be restored.
* **Validation Center** lists problems such as missing prices or invalid barcodes.

**Behind the scenes.** Each product has three separate statuses: lifecycle (draft / active / discontinued /
archived), approval (needs review / approved / rejected) and publication (published / unpublished). Only
*approved + published + active or discontinued* products can ever leave AgentReady.

---

## 4. Pricing, Inventory, Variants, Categories

**What they do.**
* **Pricing** — regular price, sale price with optional start/end dates, cost price (private, never shared),
  scheduled price changes. Every change is kept in a price history that cannot be edited.
* **Inventory** — turn on stock tracking, set quantities, record increases/decreases/reservations with a reason,
  low-stock thresholds. Every change is a logged transaction; two people changing stock at once cannot lose an update.
* **Variants** — sizes, colours, etc., each with its own SKU, price and stock.
* **Categories** — your own category tree and the attributes each category should have (for example motors need
  "Voltage"). Required attributes feed the quality score.
* **Bulk operations** — change many products at once with a preview; large jobs run in the background.

**Connects to.** Price and stock changes immediately update what AI agents see and trigger webhooks; they feed
the pricing/inventory insights. Agents only ever see "in stock / out of stock", never your quantities.

---

## 5. Synchronization and Integrations

**What they do.** **Synchronization** schedules rescans (daily/weekly/monthly in your time zone) and shows
*conflicts* — fields where your website now disagrees with your AgentReady value. You accept the website's
value, keep yours, or edit. **Integrations** connect WooCommerce or Shopify to import products and push changes.

**Status note.** WooCommerce and Shopify connections are implemented and tested against simulated stores; they
need your store credentials (and, for Shopify OAuth, a Shopify app) to work with a real store.

---

## 6. Distribution: sharing your catalog with AI agents

**What it does.** Makes your approved products available to AI shopping assistants and other systems — only if
you switch it on.

**Why it exists.** AI assistants increasingly answer "find me a…" questions. They need accurate, structured,
current product data; AgentReady gives them exactly that, and only what you allow.

**How to use it.**
1. **Distribution Overview** → switch **Public distribution** on. Choose channels: Agent API, MCP, Feeds, Webhooks.
2. **Public Products** → set your public store name and website; choose *all published products* or *selected
   ones*; exclude categories; hide prices or availability; hide attributes. **Preview** shows exactly what an agent
   receives for a product.
3. Optional: join the **public network** so agents without a key can discover your store.

**Behind the scenes.** One "public representation" is used for every channel, so every channel shows the same
data. It never contains cost prices, stock quantities, internal notes, evidence or private attributes such as
supplier. Missing values are listed as *unknown*; values you hid are listed as *withheld* — agents can tell the
difference and will not guess.

### Agent API
**Distribution → Agent API** creates keys (shown once) for agents you work with. Agents can search
(by words, brand, SKU, barcode, category, price range, attributes like "230 V", sizes/colours), get product
details, compare products, check availability and pricing, and ask for recommendations. Ranking is transparent
and never paid. Revoke a key at any time.

### MCP (Model Context Protocol)
MCP is a standard way for AI assistants to use tools. AgentReady's MCP server offers read-only tools such as
`search_products` and `get_product_details`. Connect an MCP-compatible client with the address shown in the app and
an agent key ([MCP_INTEGRATION.md](MCP_INTEGRATION.md)). It has been tested with the official MCP SDK.

### Product feeds
**Product Feeds** creates downloadable files of your catalog (JSON, JSON Lines, CSV, schema.org JSON-LD, XML), with
profiles for the OpenAI product feed and Google Merchant Center formats. **Generate now** builds a numbered
version with a validation report; **Create feed URL** gives a private link a partner can fetch. Links can expire
and be revoked. Incremental feeds contain only changes and say which products were removed.

### Webhooks
**Webhooks** sends signed messages to a partner's HTTPS address whenever a product changes (price, availability,
publication…). Failed deliveries are retried automatically, and you can redeliver.

### AI Platforms
**AI Platforms** shows, honestly, what each platform supports. ChatGPT and Google feeds can be generated and
validated here, but **submitting them requires approval and credentials from OpenAI/Google, so those connections
show as "blocked", not "connected"**. Perplexity has no merchant submission program, so nothing is sent.

### Distribution Analytics
Counts only what really happened: agent requests, MCP calls, feed downloads, webhook deliveries, checkout
hand-offs and errors, grouped by key/consumer. AgentReady cannot see what an AI platform shows its users or
whether anyone bought anything, so it does not show impressions or conversions.

### Checkout hand-off
An agent can build a cart; AgentReady checks that products are still published, prices are current and items are
available, then sends the shopper to **your own checkout** (Shopify, WooCommerce or your product page).
AgentReady never takes payments.

---

## 7. Commerce Intelligence

All intelligence is calculated from your data and real agent activity. Nothing changes your products unless you
approve it.

* **Intelligence Overview** — your catalog quality score, products needing attention and trends. **Refresh
  analysis** recalculates now (it also runs automatically every 10 minutes).
* **Catalog Quality** — a score from 0–100 per product across 12 areas (basic info, description, images,
  identifiers, pricing, category, attributes, variants, freshness, distribution readiness, structured data,
  source evidence). **Quality report** explains every lost point. It measures completeness and freshness, not
  whether your descriptions are true. Levels: excellent ≥ 85, good ≥ 70, needs improvement ≥ 50, critical < 50.
* **Discovery Insights** — what AI agents searched for in your catalog, which filters and attributes they used,
  which searches found nothing (*zero-result searches*) and why (for example "your motors have no voltage
  attribute"). These are interest signals, not sales.
* **Recommendations** — specific fixes (for example "12 motors are missing Voltage") with a priority and the
  evidence. Dismiss ones you do not want; they resolve themselves when the cause is fixed.
* **Anomaly Detection** — unusual things such as a price jump of +200 %, a missing image or a product that lost its
  price. **Investigate** opens the product.
* **Pricing / Inventory Insights** — price changes and stock patterns from your own history. There is no demand
  forecast, because AgentReady does not receive your orders.
* **Reports** — PDF, CSV or JSON reports (catalog quality, missing attributes, search performance, …) that state
  the period, data coverage and anything that could not be measured.
* **Notifications** — grouped in-app alerts (price changes, out of stock, failed feeds, …). Choose *immediate* or a
  *daily* digest in settings; e-mail delivery is not configured.

### AI Catalog Assistant
Ask questions like "Why is my catalog quality score low?". AgentReady first collects the facts from your data,
then the **Groq AI model** phrases the answer. If the AI mentions a number or product that is not in those facts,
the answer is rejected and you get the plain fact-based answer instead — so the assistant cannot make things up.
Answers show which products they are based on.

**Suggest description / attributes** drafts improvements from your own product text. The AI may not add claims
(warranties, certifications, numbers) that are not in your source. You see the old and new text side by side;
nothing changes until you click **Approve & apply**, and the change is recorded in the audit log.

---

## 8. Audit Logs and System Status

* **Audit Logs** — who changed what and when (people, the system, integrations), append-only, never containing
  passwords or keys.
* **System Status** — live checks of the services AgentReady runs on (database, job queue, background worker,
  scheduler) and your background jobs from the last 7 days: scans, imports, feed generation, bulk operations,
  webhook deliveries and reports. If something failed, it shows the reason and how to retry it.

---

## Glossary

| Term | Meaning |
|---|---|
| Evidence | The proof for a value: the page and text it came from and how it was read |
| Merchant edit | A value you changed by hand; scans never overwrite it |
| Conflict | Your website now disagrees with a value you manage; you decide |
| Published | Allowed to leave AgentReady (also needs approval and distribution) |
| Agent | Software (often an AI assistant) that searches products for a person |
| MCP | Model Context Protocol, a standard for AI assistants to use tools |
| Feed | A file of your catalog that another system downloads |
| Webhook | A message AgentReady sends to another system when something changes |
| Groq | The AI service AgentReady uses for reading difficult pages and phrasing answers |
