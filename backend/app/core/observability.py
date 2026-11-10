"""Logging, request context and HTTP hardening shared by the API process and the workers.

* Secrets are redacted from every log record (feed tokens in URLs, catalog API keys, webhook secrets,
  Groq keys, bearer tokens, reset tokens in query strings) before any handler formats it.
* ``LOG_FORMAT=json`` writes one JSON object per line (timestamp, level, logger, message, request_id and
  structured extras such as ``duration_ms``); ``text`` stays human readable.
* ``RequestContextMiddleware`` (pure ASGI, so streaming MCP responses are untouched) assigns a request ID,
  measures latency (``Server-Timing`` header + one structured log line per request), refuses oversized
  request bodies with 413 and sets security headers.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from app.core.config import Settings

REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(/api/v3/feeds/)arf_[A-Za-z0-9_-]+"), r"\1[redacted]"),
    (re.compile(r"\b(arf_|ar_cat_|gsk_)[A-Za-z0-9_-]{8,}"), r"\1[redacted]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9+/=]{8,}"), "whsec_[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{8,}=*"), r"\1[redacted]"),
    (re.compile(r"(?i)([?&](?:token|access_token|key|api_key|secret)=)[^&\s\"']+"), r"\1[redacted]"),
]


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed record: leave it to the handler
            return True
        clean = redact(msg)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from app.services.commerce.audit import request_id_var

        if not getattr(record, "request_id", None):
            record.request_id = request_id_var.get() or "-"
        return True


_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "request_id"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for k, v in vars(record).items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                out[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
        if record.exc_info:
            out["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


_configured = False
_redact = RedactSecretsFilter()
_rid = RequestIdFilter()


def configure_logging(s: Settings) -> None:
    """Idempotent. Installs redaction on the root handler and on loggers that do not propagate."""
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler()
        root.addHandler(handler)
        _configured = True
    root.setLevel(s.log_level)
    fmt: logging.Formatter = (JsonFormatter() if s.log_format == "json" else
                              logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"))
    for h in root.handlers:
        h.setFormatter(fmt)
        for f in (_rid, _redact):
            if f not in h.filters:
                h.addFilter(f)
    # uvicorn/celery loggers may have their own handlers; filter at the logger as well.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "agentready", "httpx", "celery", "celery.task"):
        lg = logging.getLogger(name)
        for f in (_rid, _redact):
            if f not in lg.filters:
                lg.addFilter(f)
        for h in lg.handlers:
            h.setFormatter(fmt)
    if s.log_format == "json":
        # RequestContextMiddleware writes a structured access line; avoid the duplicate text line.
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


_RID_OK = re.compile(r"[A-Za-z0-9._-]{8,64}")
access_log = logging.getLogger("agentready.http")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
NO_STORE_PREFIXES = ("/api/v1", "/api/v2", "/api/v3/agents", "/api/v3/mcp")


class RequestContextMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.s = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from app.services.commerce.audit import request_id_var

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        rid = headers.get(b"x-request-id", b"").decode("latin-1")
        rid = rid if _RID_OK.fullmatch(rid) else secrets.token_hex(8)
        token = request_id_var.set(rid)
        # exception handlers run in Starlette's outermost ServerErrorMiddleware, after this middleware has
        # returned: give them the request ID through the scope state
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        path = scope.get("path", "")
        status_holder = {"status": 500}
        limit = self.s.max_request_body_bytes

        async def _reject(status: int, code: str, message: str):
            body = json.dumps({"error": {"code": code, "message": message, "details": {}}}).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                    (b"x-request-id", rid.encode())]})
            await send({"type": "http.response.body", "body": body})
            status_holder["status"] = status

        try:
            declared = headers.get(b"content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                return await _reject(413, "payload_too_large", f"Request body exceeds {limit} bytes")

            received = 0
            too_large = {"hit": False, "sent": False}

            async def limited_receive():
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        too_large["hit"] = True
                        raise _BodyTooLarge()
                return message

            async def send_wrapper(message):
                if too_large["hit"]:
                    # the framework turned the aborted body read into its own error: answer 413 instead
                    if not too_large["sent"]:
                        too_large["sent"] = True
                        await _reject(413, "payload_too_large", f"Request body exceeds {limit} bytes")
                    return
                if message["type"] == "http.response.start":
                    status_holder["status"] = message["status"]
                    existing = {k.lower() for k, _ in message.get("headers", [])}
                    extra = [(b"x-request-id", rid.encode())] if b"x-request-id" not in existing else []
                    extra += [h for h in SECURITY_HEADERS if h[0] not in existing]
                    if path.startswith(NO_STORE_PREFIXES) and b"cache-control" not in existing:
                        extra.append((b"cache-control", b"no-store"))
                    dur = (time.perf_counter() - start) * 1000
                    extra.append((b"server-timing", f"app;dur={dur:.1f}".encode()))
                    message = {**message, "headers": list(message.get("headers", [])) + extra}
                await send(message)

            try:
                await self.app(scope, limited_receive, send_wrapper)
            except _BodyTooLarge:
                if not too_large["sent"]:
                    too_large["sent"] = True
                    await _reject(413, "payload_too_large", f"Request body exceeds {limit} bytes")
        finally:
            dur = round((time.perf_counter() - start) * 1000, 1)
            status = status_holder["status"]
            if path not in ("/health", "/health/live", "/health/ready"):
                level = logging.WARNING if status >= 500 or dur > 2000 else logging.INFO
                access_log.log(level, "%s %s -> %s in %sms", scope.get("method"), redact(path), status, dur,
                               extra={"http_method": scope.get("method"), "http_path": redact(path),
                                      "http_status": status, "duration_ms": dur})
            request_id_var.reset(token)


class _BodyTooLarge(Exception):
    pass
