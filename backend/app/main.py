from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth, catalog, dashboard, exports, products, scans, settings, validation, websites
from app.api.v1 import commerce, integrations, pricing_inventory, sync_api
from app.api.v1 import distribution, intelligence, system
from app.api.v2 import catalog as catalog_v2
from app.api.v3 import agents as agents_v3
from app.api.v3 import feeds as feeds_v3
from app.api.v3 import mcp as mcp_v3
from app.core.config import get_settings
from app.core.errors import install_domain_error_handlers, install_error_handlers

from app.core.observability import RequestContextMiddleware, configure_logging

s = get_settings()
configure_logging(s)
log = logging.getLogger("agentready")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if s.ai_provider == "groq" and not s.groq_api_key:
        log.warning("GROQ_API_KEY is not set: scans will use deterministic extraction only")
    if s.ai_provider == "mock":
        log.warning("AI_PROVIDER=mock: AI results come from the LOCAL MOCK provider, not a real model")
    if s.allow_private_networks:
        log.warning("ALLOW_PRIVATE_NETWORKS=true: SSRF protection for private ranges is DISABLED (dev only)")
    from app.core.health import check_database

    db = await check_database()
    if db["status"] != "ok":
        # Start anyway: requests get 503 + Retry-After and /health reports the outage until it recovers.
        log.error("Database is not reachable at startup (%s); serving 503 until it recovers", db.get("error"))
    scheduler = None
    if s.job_backend == "inline" and s.scheduler_enabled:
        from app.workers.scheduler import start_inline_scheduler

        scheduler = start_inline_scheduler()
    yield
    if scheduler is not None:
        for t in scheduler if isinstance(scheduler, (list, tuple)) else (scheduler,):
            t.cancel()


app = FastAPI(
    title="AgentReady API",
    version="0.1.0",
    description="Turn existing product websites into validated, machine-readable catalogs.",
    lifespan=lifespan,
    openapi_url=f"{s.api_prefix}/openapi.json" if s.docs_enabled else None,
    docs_url="/docs" if s.docs_enabled else None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=s.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Catalog-Key", "X-Request-ID", "MCP-Protocol-Version"],
    expose_headers=["X-Request-ID"],
)
install_error_handlers(app)
install_domain_error_handlers(app)


# Outermost: request ID, latency, body-size limit and security headers for every response.
app.add_middleware(RequestContextMiddleware, settings=s)


for r in (auth.router, settings.router, websites.router, scans.router, products.router, validation.router,
          exports.router, catalog.router, dashboard.router, commerce.router, pricing_inventory.router, sync_api.router,
          integrations.router, distribution.router, intelligence.router, system.router):
    app.include_router(r, prefix=s.api_prefix)
app.include_router(catalog_v2.router, prefix="/api/v2")
app.include_router(agents_v3.router, prefix="/api/v3")
app.include_router(feeds_v3.router, prefix="/api/v3")
app.include_router(mcp_v3.router, prefix="/api/v3")


@app.exception_handler(agents_v3.AgentError)
async def _agent_error(_: Request, exc: agents_v3.AgentError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message,
                                                                   "details": exc.details}})


@app.get("/.well-known/agentready.json", tags=["meta"], include_in_schema=False)
async def well_known():
    """Agent capability discovery document (also at /api/v3/agents/capabilities)."""
    return agents_v3.capability_document()


@app.api_route("/health/live", methods=["GET", "HEAD"], tags=["meta"])
async def health_live():
    """Liveness: the API process is running and serving requests (no dependency checks)."""
    return {"status": "ok"}


@app.get("/health", tags=["meta"])
@app.get("/health/ready", tags=["meta"], include_in_schema=False)
async def health():
    """Readiness: real database / Redis / worker / scheduler checks. 503 when the database or Redis is down."""
    from fastapi.responses import JSONResponse

    from app.core.health import run_checks

    code, body = await run_checks()
    return JSONResponse(status_code=code, content=body, headers={"Cache-Control": "no-store"})


# Optional: serve the built frontend from this process (single-service hosting such as Render's free plan).
# Docker Compose deployments leave STATIC_DIR unset and serve the SPA from nginx instead.
if s.static_dir:
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    _static = Path(s.static_dir).resolve()
    if (_static / "index.html").is_file():
        if (_static / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

        # HEAD too: Render's port probe and uptime monitors send HEAD /
        @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith(("api/", "health", "docs", ".well-known/")):
                raise HTTPException(status_code=404)
            candidate = (_static / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(_static):
                return FileResponse(candidate)
            # index.html must never be cached: a deploy replaces the hashed asset names it references
            return FileResponse(_static / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        log.warning("STATIC_DIR=%s has no index.html; frontend is not served", _static)
