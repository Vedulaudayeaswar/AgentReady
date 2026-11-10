# AI commerce platforms: what is officially supported (researched November 2026)

AgentReady never reports a platform as connected or active without evidence. Generating a feed or schema.org
JSON-LD does **not** by itself make products appear in ChatGPT, Perplexity, Google or any other AI platform; each
platform decides inclusion under its own programme. Sources below are the platforms' own documentation; re-check
them before relying on any detail, because these programmes change frequently.

## ChatGPT (OpenAI): Product Feed spec and Agentic Commerce Protocol

Sources: <https://developers.openai.com/commerce>, <https://developers.openai.com/commerce/specs/file-upload/products>,
<https://developers.openai.com/commerce/specs/file-upload/overview>, <https://chatgpt.com/merchants>.

- **Onboarding.** Only approved partners can onboard product feeds; merchants apply at chatgpt.com/merchants. The
  pages read do not state the approval criteria. The standard upload targets the US.
- **Delivery and formats.**
  - Feeds are pushed over SFTP, using credentials issued during onboarding (no public self-serve API).
  - Formats: parquet (preferred), jsonl.gz, csv.gz or tsv.gz, UTF-8.
  - Full snapshots at least daily, starting with a sample of about 100 items.
- **Required fields:** item_id, title (about 150 characters max), description (plain text, about 5,000 characters max),
  url, brand, seller_name, image_url, availability and price.
  - availability is one of in_stock, out_of_stock, pre_order, backorder or unknown.
  - price is written like "79.99 USD".
  - Rows with a missing or invalid required field are rejected.
- **Optional fields:**
  - sale_price, gtin (valid check digit only), mpn
  - group_id, listing_has_variations, variant_dict
  - product_category, material, color, size
  - additional_image_urls, seller_url
  - return_policy, seller_privacy_policy, seller_tos
  - is_eligible_search, is_eligible_checkout
- **Checkout.** The Agentic Commerce Protocol is a separately enabled integration:
  - The merchant implements the checkout_sessions endpoints and is the merchant of record.
  - Delegated Payment is limited to payment providers and PCI DSS level 1 merchants.

**AgentReady status.**

| Capability | Status |
|---|---|
| Generate a feed in OpenAI's field names (jsonl or csv) and validate it against these rules (rows missing required data are excluded and listed; nothing is invented) | PASS |
| Submission over SFTP (needs OpenAI approval and the SFTP credentials it issues) | BLOCKED |

## Google: Merchant Center and Universal Commerce Protocol (UCP)

Sources: <https://ucp.dev>, <https://developers.google.com/merchant/ucp>, <https://developers.google.com/merchant/ucp/faq>,
<https://developers.google.com/merchant/api>.

- **Prerequisites.**
  - A Merchant Center account in good standing with approved free listings.
  - A return policy and a customer support contact configured in Merchant Center.
- **UCP checkout.**
  - Early access for selected US merchants: submit the interest form, then implement UCP.
  - REST is the recommended transport; MCP and A2A are also defined by the spec.
  - The business profile is published at `/.well-known/ucp`.
  - Items opt in with `native_commerce(checkout_eligibility)`.
- **Catalog updates.** Through Merchant API data sources (Google OAuth). The Content API for Shopping is marked deprecated.

**AgentReady status.**

| Capability | Status |
|---|---|
| Generate and validate a Merchant Center feed (RSS 2.0 XML with the `g:` namespace, or csv), which can be added to Merchant Center as a scheduled-fetch data source | PASS |
| Merchant API submission (needs a Google Cloud OAuth client and merchant authorization) | BLOCKED |
| UCP checkout endpoints (need Google approval; not implemented) | BLOCKED |

## Perplexity

Sources: <https://docs.perplexity.ai>, <https://www.perplexity.ai/merchants>.

Perplexity's developer documentation lists the Search, Agent, Decisions, Orchestrator and Embeddings APIs, and no
merchant catalog submission API. No official merchant feed specification was found.

Third-party articles claim Perplexity reads Google-format feeds. That claim is unverified, so AgentReady does not rely on it.

**AgentReady status:** no documented integration exists, so nothing is submitted (BLOCKED).

## Independent MCP-compatible clients

AgentReady's own MCP server is at `/api/v3/mcp`. It uses the Streamable HTTP transport with JSON responses.

**Supported protocol versions.**

| Era | Versions |
|---|---|
| Stateless (current) | 2026-07-28 |
| Handshake | 2025-11-25, 2025-06-18, 2025-03-26 |

**Verified with real clients.**

| Client | Protocol negotiated | Result |
|---|---|---|
| Official MCP Python SDK 2.3.0, mode `auto` | 2026-07-28 | PASS |
| Official MCP Python SDK 2.3.0, mode `legacy` | 2025-11-25 | PASS |
| Official MCP Python SDK 1.16.0 | 2025-06-18 | PASS |
| MCP Inspector 2.12.0 CLI (TypeScript SDK) | — | PASS: listed and called the tools |

The platform status becomes **active** only when AgentReady has served real MCP traffic for the merchant in the last 7 days.
