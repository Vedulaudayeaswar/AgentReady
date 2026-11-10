"""Verify AgentReady's MCP server with the OFFICIAL MCP Python SDK client (not hand-written JSON).

Works with SDK 2.x (``mcp.Client``; runs both the 2026-07-28 stateless era and the legacy initialize handshake)
and SDK 1.x (``ClientSession`` over ``streamablehttp_client``; handshake era).

    AR_MCP_URL=http://localhost:8000/api/v3/mcp AR_KEY=ar_cat_... python eval/mcp_client_check.py

Exit code 0 only if every tool call succeeded against real data. Prints the negotiated protocol version.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

URL = os.environ.get("AR_MCP_URL", "http://localhost:8000/api/v3/mcp")
KEY = os.environ.get("AR_KEY", "")
QUERY = os.environ.get("AR_QUERY", "")
EXPECTED_TOOLS = {"search_products", "get_product_details", "compare_products", "get_product_variants",
                  "check_product_availability", "get_product_pricing", "search_merchants", "get_merchant_catalog"}


def _structured(res) -> dict:
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc:
        return sc
    return json.loads(res.content[0].text)


async def exercise(call, list_tools, label: str, version: str) -> list[str]:
    log = [f"[{label}] negotiated protocol version: {version}"]
    tools = await list_tools()
    names = {t.name for t in tools}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"missing tools: {missing}"
    log.append(f"[{label}] tools/list: {len(names)} tools ({', '.join(sorted(names))})")
    res = await call("search_products", {"query": QUERY} if QUERY else {"page_size": 5})
    assert not res.isError if hasattr(res, "isError") else not res.is_error, res
    data = _structured(res)
    items = data["items"]
    assert items, "search returned no products (is distribution enabled and are products approved?)"
    log.append(f"[{label}] search_products -> {data['total']} result(s); top: {items[0]['product']['name']!r} "
               f"score {items[0]['ranking']['score']}")
    pid = items[0]["product"]["id"]
    det = _structured(await call("get_product_details", {"product_id": pid}))
    log.append(f"[{label}] get_product_details -> {det['product']['name']!r}, unknown fields {det['product']['unknown_fields']}")
    pr = _structured(await call("get_product_pricing", {"product_id": pid}))
    log.append(f"[{label}] get_product_pricing -> {pr['price']}")
    av = _structured(await call("check_product_availability", {"product_id": pid}))
    log.append(f"[{label}] check_product_availability -> {av['availability']}")
    va = _structured(await call("get_product_variants", {"product_id": pid}))
    log.append(f"[{label}] get_product_variants -> {len(va['variants'])} variant(s)")
    if len(items) > 1:
        cmp_ = _structured(await call("compare_products", {"product_ids": [items[0]["product"]["id"], items[1]["product"]["id"]]}))
        log.append(f"[{label}] compare_products -> {len(cmp_['comparison']['rows'])} comparison rows")
    ms = _structured(await call("search_merchants", {}))
    log.append(f"[{label}] search_merchants -> {[m['name'] for m in ms['items']]}")
    bad = await call("get_product_details", {"product_id": "00000000-0000-0000-0000-000000000000"})
    is_err = getattr(bad, "isError", None)
    is_err = bad.is_error if is_err is None else is_err
    assert is_err, "a missing product must be a tool error"
    log.append(f"[{label}] unknown product -> isError=True (tool execution error, as the spec requires)")
    return log


async def run_v2() -> list[str]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    out = []
    for mode in ("auto", "legacy"):
        headers = {"Authorization": f"Bearer {KEY}"} if KEY else {}
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            async with Client(streamable_http_client(URL, http_client=http), mode=mode) as c:
                async def lt():
                    return (await c.list_tools()).tools

                async def call(name, args):
                    return await c.call_tool(name, args)

                out += await exercise(call, lt, f"sdk {mode}", c.protocol_version)
    return out


async def run_v1() -> list[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {KEY}"} if KEY else {}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as s:
            init = await s.initialize()

            async def lt():
                return (await s.list_tools()).tools

            async def call(name, args):
                return await s.call_tool(name, args)

            return await exercise(call, lt, "sdk v1 handshake", init.protocolVersion)


def main() -> int:
    import importlib.metadata as md

    version = md.version("mcp")
    print(f"official MCP Python SDK {version} -> {URL}")
    try:
        import mcp

        lines = asyncio.run(run_v2() if hasattr(mcp, "Client") else run_v1())
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1
    print("\n".join(lines))
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
