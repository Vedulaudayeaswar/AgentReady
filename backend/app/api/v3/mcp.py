"""Model Context Protocol server (Streamable HTTP transport) at ``/api/v3/mcp``.

Supports both protocol eras:

* **2026-07-28** (current, stateless): ``server/discover``; every request carries
  ``_meta["io.modelcontextprotocol/protocolVersion"]`` matching the ``MCP-Protocol-Version`` header; results carry
  ``resultType`` and ``_meta["io.modelcontextprotocol/serverInfo"]``.
* **2025-11-25 / 2025-06-18 / 2025-03-26** (handshake): ``initialize`` → ``notifications/initialized``; the server is
  stateless and issues no ``Mcp-Session-Id``.

Responses are single JSON objects (``Content-Type: application/json``); GET returns 405 (no server-initiated SSE
stream); notifications get 202. The ``Origin`` header is validated (DNS-rebinding protection). Tools are the same
read-only registry as ``/api/v3/agents/tools``, so MCP calls go through exactly the same authorization, public
serializer, rate limits and analytics as the REST API.

Authentication: ``Authorization: Bearer <API key with agent:read>`` or ``X-Catalog-Key``; without credentials the
client sees the public network.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import bearer
from app.core.config import get_settings
from app.db.session import get_db
from app.services.distribution import tools
from app.services.distribution.agent import API_VERSION, AgentError

log = logging.getLogger("agentready.mcp")
router = APIRouter(tags=["MCP"])

MODERN_VERSIONS = ["2026-07-28"]
HANDSHAKE_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
ALL_VERSIONS = MODERN_VERSIONS + HANDSHAKE_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
SERVER_INFO = {"name": "agentready", "title": "AgentReady product catalog", "version": API_VERSION,
               "description": "Search, compare and check merchant-approved products"}
INSTRUCTIONS = ("AgentReady serves merchant-approved product data. Use search_products to find products (filters: brand, "
                "price range with currency, availability, attributes like 'voltage:230 V', variant options like 'size:M'), "
                "get_product_details/get_product_pricing/check_product_availability for current facts, and compare_products "
                "to compare. Values listed in unknown_fields are not known — do not guess them. Prices are never converted "
                "between currencies.")
CAPABILITIES = {"tools": {"listChanged": False}}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022


def _err(id_, code: int, message: str, data=None, status: int = 200) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message, **({"data": data} if data is not None else {})}}
    return JSONResponse(body, status_code=status)


def _ok(id_, result: dict, modern: bool) -> JSONResponse:
    if modern:
        result = {**result, "resultType": result.get("resultType", "complete"),
                  "_meta": {**(result.get("_meta") or {}), META_SERVER_INFO: SERVER_INFO}}
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _origin_ok(origin: str | None) -> bool:
    if not origin:
        return True  # non-browser clients send no Origin
    s = get_settings()
    allowed = set(s.cors_origin_list) | {s.api_base_url.rstrip("/"), s.frontend_url.rstrip("/")}
    return origin.rstrip("/") in allowed


def _tool_list() -> list[dict]:
    return [t.definition() for t in tools.TOOLS.values()]


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                   x_catalog_key: str | None = Header(default=None, alias="X-Catalog-Key"),
                   mcp_protocol_version: str | None = Header(default=None, alias="MCP-Protocol-Version"),
                   db=Depends(get_db)):
    if not _origin_ok(request.headers.get("origin")):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Origin not allowed"}},
                            status_code=403)
    raw = await request.body()
    if len(raw) > 1_000_000:
        return _err(None, INVALID_REQUEST, "Request too large", status=413)
    try:
        msg = json.loads(raw or b"null")
    except ValueError:
        return _err(None, PARSE_ERROR, "Parse error", status=400)
    if isinstance(msg, list):
        return _err(None, INVALID_REQUEST, "JSON-RPC batches are not supported", status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid JSON-RPC message", status=400)
    if mcp_protocol_version and mcp_protocol_version not in ALL_VERSIONS:
        return _err(msg.get("id"), UNSUPPORTED_VERSION, f"Unsupported protocol version {mcp_protocol_version}",
                    {"supported": ALL_VERSIONS, "requested": mcp_protocol_version}, status=400)
    method = msg.get("method")
    if method is None or "id" not in msg:
        return Response(status_code=202)  # notification or client response: accepted, nothing to return
    id_, params = msg.get("id"), msg.get("params") or {}
    if not isinstance(params, dict):
        return _err(id_, INVALID_PARAMS, "params must be an object")
    meta_version = (params.get("_meta") or {}).get(META_VERSION) if isinstance(params.get("_meta"), dict) else None
    modern = meta_version is not None or method == "server/discover"
    if modern:
        version = meta_version or mcp_protocol_version
        if mcp_protocol_version and meta_version and mcp_protocol_version != meta_version:
            return _err(id_, HEADER_MISMATCH, "MCP-Protocol-Version header does not match _meta protocolVersion", status=400)
        if version not in MODERN_VERSIONS:
            return _err(id_, UNSUPPORTED_VERSION, f"Unsupported protocol version {version}",
                        {"supported": ALL_VERSIONS, "requested": version})

    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = requested if requested in HANDSHAKE_VERSIONS else HANDSHAKE_VERSIONS[0]
        return _ok(id_, {"protocolVersion": negotiated, "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO,
                         "instructions": INSTRUCTIONS}, False)
    if method == "server/discover":
        keyed = bool(x_catalog_key or creds)
        return _ok(id_, {"supportedVersions": ALL_VERSIONS, "capabilities": CAPABILITIES, "instructions": INSTRUCTIONS,
                         "cacheScope": "private" if keyed else "public", "ttlMs": 300_000}, True)
    if method == "ping":
        return _ok(id_, {}, modern)
    if method == "tools/list":
        res: dict = {"tools": _tool_list()}
        if modern:
            res.update(cacheScope="public", ttlMs=300_000)
        return _ok(id_, res, modern)
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in tools.TOOLS:
            return _err(id_, INVALID_PARAMS, f"Unknown tool: {name}")
        if not isinstance(args, dict):
            return _err(id_, INVALID_PARAMS, "arguments must be an object")
        from app.api.v3.agents import resolve_agent

        try:
            actx = await resolve_agent(request, creds, x_catalog_key, db, "mcp")
        except HTTPException as e:
            # authentication/authorization failures are protocol-level: the client must fix its credentials
            return _err(id_, INVALID_REQUEST if e.status_code != 429 else INTERNAL, str(e.detail),
                        {"http_status": e.status_code}, status=e.status_code if e.status_code in (401, 403, 429) else 400)
        try:
            data = await tools.call(db, actx, name, args)
        except tools.ToolInputError as e:
            return _ok(id_, {"content": [{"type": "text", "text": str(e)}], "isError": True}, modern)
        except AgentError as e:
            from app.services.distribution.agent import record_error

            await record_error(db, actx, name, e.status, e.message)
            return _ok(id_, {"content": [{"type": "text", "text": json.dumps({"error": {"code": e.code, "message": e.message,
                                                                                      **({"details": e.details} if e.details else {})}})}],
                             "isError": True}, modern)
        except Exception:  # noqa: BLE001
            log.exception("MCP tool %s failed", name)
            return _ok(id_, {"content": [{"type": "text", "text": "Internal error while running the tool"}], "isError": True}, modern)
        text = json.dumps(data, ensure_ascii=False, default=str)
        return _ok(id_, {"content": [{"type": "text", "text": text}], "structuredContent": json.loads(text),
                         "isError": False}, modern)
    return _err(id_, METHOD_NOT_FOUND, f"Method not found: {method}")
