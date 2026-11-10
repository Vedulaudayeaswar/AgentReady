"""Consistent error envelope: {"error": {"code", "message", "details"}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("agentready")


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(what: str = "Resource") -> AppError:
    return AppError(404, "not_found", f"{what} not found")


def _body(code: str, message: str, details: dict | list | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=_body(exc.code, exc.message, exc.details))

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException):
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content=_body("validation_error", "Invalid request", {"errors": errors}))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        from app.services.commerce.audit import request_id_var

        rid = getattr(request.state, "request_id", None) or request_id_var.get()
        headers = {"X-Request-ID": rid} if rid else {}
        if is_database_unavailable(exc):
            log.error("Database unavailable: %s", type(exc).__name__, extra={"request_id": rid})
            return JSONResponse(status_code=503, headers={**headers, "Retry-After": "5"}, content=_body(
                "service_unavailable", "The database is temporarily unavailable. Please retry shortly.",
                {"request_id": rid}))
        log.exception("Unhandled error", exc_info=exc, extra={"request_id": rid})
        # The request ID lets support find the full traceback in the server log; nothing internal is returned.
        return JSONResponse(status_code=500, headers=headers, content=_body(
            "internal_error", "An unexpected error occurred", {"request_id": rid}))


def is_database_unavailable(exc: BaseException) -> bool:
    """Connection-level failures (database down, pool exhausted) as opposed to bugs in a query."""
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    if isinstance(exc, (PoolTimeout, OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    if isinstance(exc, OSError):
        # asyncpg raises socket errors (ConnectionRefusedError, gaierror) unwrapped while the pool
        # connects; only treat an OSError as a database outage when it was raised from that code.
        tb = exc.__traceback__
        while tb is not None:
            mod = tb.tb_frame.f_globals.get("__name__", "")
            if mod.startswith(("asyncpg", "sqlalchemy.pool", "sqlalchemy.engine", "sqlalchemy.dialects")):
                return True
            tb = tb.tb_next
    return False


def install_domain_error_handlers(app: FastAPI) -> None:
    """Map service-layer rule violations to the standard error envelope (messages are merchant-safe)."""
    from app.services.commerce.catalog_rules import CatalogRuleError
    from app.services.commerce.inventory import InventoryConflict, InventoryError
    from app.services.commerce.lifecycle import LifecycleError
    from app.services.commerce.money import PricingError

    @app.exception_handler(PricingError)
    async def _pricing(_: Request, exc: PricingError):
        return JSONResponse(status_code=422, content=_body("pricing_rule", str(exc)))

    @app.exception_handler(InventoryConflict)
    async def _inv_conflict(_: Request, exc: InventoryConflict):
        return JSONResponse(status_code=409, content=_body("inventory_conflict", str(exc)))

    @app.exception_handler(InventoryError)
    async def _inventory(_: Request, exc: InventoryError):
        return JSONResponse(status_code=422, content=_body("inventory_rule", str(exc)))

    @app.exception_handler(CatalogRuleError)
    async def _catalog(_: Request, exc: CatalogRuleError):
        return JSONResponse(status_code=exc.status, content=_body(exc.code, str(exc)))

    @app.exception_handler(LifecycleError)
    async def _lifecycle(_: Request, exc: LifecycleError):
        return JSONResponse(status_code=exc.status, content=_body(exc.code, str(exc)))
