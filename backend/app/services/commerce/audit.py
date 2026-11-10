"""Audit trail and actor identity.

Audit entries never contain secrets: values under keys that look like credentials are
replaced before storage, and long values are truncated.
"""

from __future__ import annotations

import contextvars
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

SECRET_KEY_PATTERN = re.compile(r"(password|secret|token|api[_-]?key|credential|consumer[_-]?key|authorization)", re.I)
SAFE_KEYS = {"catalog_api_key_prefix", "prefix", "credentials_hint"}


@dataclass(frozen=True)
class Actor:
    type: str  # user | system | api_key | integration
    id: str | None = None
    label: str | None = None

    @staticmethod
    def system(label: str = "AgentReady") -> "Actor":
        return Actor("system", None, label)


def actor_from_ctx(ctx) -> Actor:
    if ctx.user is not None:
        return Actor("user", str(ctx.user.id), ctx.user.email)
    return Actor("api_key", (ctx.claims or {}).get("api_key_id"), "API key")


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if isinstance(v, str) and len(v) > 2000:
        return v[:2000] + "…"
    return v


def scrub(changes: dict | None) -> dict:
    """Remove anything that looks like a secret. Values are truncated to keep entries small."""
    out: dict = {}
    for k, v in (changes or {}).items():
        if k not in SAFE_KEYS and SECRET_KEY_PATTERN.search(str(k)):
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = scrub(v)
        else:
            out[k] = jsonable(v)
    return out


def record(db: AsyncSession, business_id: uuid.UUID, actor: Actor, action: str, entity_type: str,
           entity_id: Any, summary: str, changes: dict | None = None, operation_id: Any = None) -> AuditLog:
    entry = AuditLog(
        business_id=business_id, actor_type=actor.type, actor_id=actor.id, actor_label=actor.label, action=action,
        entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None, summary=summary[:2000],
        changes=scrub(changes), request_id=request_id_var.get(),
        operation_id=str(operation_id) if operation_id is not None else None,
    )
    db.add(entry)
    return entry


def diff(before: dict, after: dict) -> dict:
    """{field: {"from": a, "to": b}} for changed keys."""
    return {k: {"from": jsonable(before.get(k)), "to": jsonable(after.get(k))}
            for k in after if before.get(k) != after.get(k)}
