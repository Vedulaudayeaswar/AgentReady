# MCP integration

AgentReady runs a Model Context Protocol (MCP) server so MCP-compatible AI clients can search and read a
merchant's **published, distribution-approved** catalog. It is read-only: there are no tools that change prices,
inventory, publication or anything else.

## Endpoint and transport

| | |
|---|---|
| URL | `https://<your-domain>/api/v3/mcp` (local Docker: `http://localhost:8080/api/v3/mcp`, API directly: `http://localhost:8000/api/v3/mcp`) |
| Transport | Streamable HTTP, `POST` JSON-RPC 2.0; responses are JSON (`Accept: application/json, text/event-stream`) |
| Sessions | stateless (no `Mcp-Session-Id`); `GET` (SSE stream) returns 405, `DELETE` returns 405 |
| Protocol versions | **2026-07-28** (stateless, `server/discover`, version in `_meta`) and the handshake era **2025-11-25**, **2025-06-18**, **2025-03-26** (`initialize` → `notifications/initialized`). Unknown versions get a counter-offer during `initialize`; an unsupported `MCP-Protocol-Version` header is refused. |
| Capabilities | `tools` only (no resources, prompts or sampling) |

## Authentication

1. The merchant switches on **Distribution → Overview → Public distribution** and keeps the **MCP** channel enabled.
2. The merchant creates a key in **Distribution → Agent API** with the `agent:read` scope (shown once).
3. The client sends it as `Authorization: Bearer ar_cat_…` **or** `X-Catalog-Key: ar_cat_…`.

Without credentials, only merchants that opted into the **public network** are visible (rate limited per IP).
With a key, only that merchant's catalog is visible. Revoking the key stops access immediately.

## Tools

| Tool | What it returns |
|---|---|
| `search_products` | Products matching keywords, brand, SKU, GTIN, category, price range (in one currency), availability, attributes (`voltage:230 V`, `storage>=512 GB`) and variant options; ranked with reasons |
| `get_product_details` | Full public details of one product |
| `compare_products` | 2–10 products side by side (price, currency, brand, availability, shipping, variants, unit-aware attributes) |
| `get_product_variants` | Purchasable variants with options, price and availability |
| `check_product_availability` | Availability status of a product and its variants (never stock quantities) |
| `get_product_pricing` | Regular and sale price in the merchant's currency, sale dates, next scheduled change |
| `search_merchants` | Merchants whose catalogs the caller can access |
| `get_merchant_catalog` | Page through one merchant's distributed catalog |
| `recommend_products` | Match a plain-language request to real products (deterministic parsing; no invented products) |

Every tool has an `inputSchema` and an `outputSchema`, `annotations.readOnlyHint: true`, and returns both
`structuredContent` and a JSON text block. Tool errors (bad arguments, unknown product) come back as
`isError: true` results; protocol errors use JSON-RPC error codes (`-32602` invalid params/unknown tool, `-32601`
method not found). The same tools are available over REST at `GET /api/v3/agents/tools` and
`POST /api/v3/agents/tools/{name}`.

Data policy: the tools use the same public serializer as the agent API and feeds — no cost prices, stock
quantities, notes, evidence or private attributes; `unknown_fields` and `withheld_fields` tell the model what is
missing versus withheld.

## Client configuration

Claude Code, Cursor, VS Code and other clients that accept an HTTP MCP server:

```json
{
  "mcpServers": {
    "agentready": {
      "type": "http",
      "url": "https://your-domain.example/api/v3/mcp",
      "headers": { "Authorization": "Bearer ar_cat_your_agent_key" }
    }
  }
}
```

Claude Code CLI:

```bash
claude mcp add --transport http agentready https://your-domain.example/api/v3/mcp \
  --header "Authorization: Bearer ar_cat_your_agent_key"
```

Official MCP Python SDK:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("https://your-domain.example/api/v3/mcp",
                                 headers={"Authorization": "Bearer ar_cat_..."}) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("search_products", {"query": "cordless drill"})
```

Raw HTTP (handshake era):

```bash
curl -s https://your-domain.example/api/v3/mcp -H "Authorization: Bearer ar_cat_..." \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_products","arguments":{"query":"drill"}}}'
```

## Verification status

| Client | Protocol negotiated | Result | Evidence |
|---|---|---|---|
| Official MCP Python SDK 2.3.0 (`auto`) | 2026-07-28 | PASS | `eval/mcp_client_check.py`, Phase 3 walkthrough step 14 (re-run in Phase 5 against Docker) |
| Official MCP Python SDK 2.3.0 (`legacy`) | 2025-11-25 | PASS | same |
| Official MCP Python SDK 1.16.0 | 2025-06-18 | PASS | Phase 3 |
| MCP Inspector 2.12.0 CLI (TypeScript SDK) | handshake | PASS | Phase 3 |
| Protocol conformance (handshake, both eras, errors, no privileged tools) | — | PASS | `test_distribution.py::test_mcp_streamable_http_protocol` |

Run the check yourself against any stack:

```bash
cd backend
python -m venv .venv-mcp && .venv-mcp/Scripts/pip install -r requirements-mcp-client.txt   # macOS/Linux: .venv-mcp/bin/pip
AR_MCP_URL=http://localhost:8080/api/v3/mcp AR_KEY=ar_cat_... .venv-mcp/Scripts/python eval/mcp_client_check.py
```

**Not claimed:** being reachable from ChatGPT, Gemini, Perplexity or any hosted AI product. Those require the
platform to support remote MCP servers for this use and, for commerce, their own approval programmes — see
[docs/phase3-platforms.md](docs/phase3-platforms.md). Having an MCP endpoint does not by itself mean any platform
uses it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` | missing/revoked key, or the key lacks `agent:read` |
| `403 distribution_disabled` | the merchant has not switched on public distribution or the MCP channel |
| tool result `isError: true` with `not_found` | the product is not published/approved/distributed (same answer as a missing id) |
| `-32022 Unsupported protocol version` | the client sent a version header the server does not support; upgrade the client or use one listed above |
| `429` | rate limit for the key (default 120/min) or anonymous IP (60/min) |
