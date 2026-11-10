"""Product lifecycle, approval and publication rules.

* lifecycle_status: draft | active | archived | discontinued (what the product IS)
* approval_status:  needs_review | approved | rejected        (has the merchant verified the data)
* publication_status: unpublished | published                 (is it exposed through feeds/the catalog API)

Publishing requires approval and an active/discontinued lifecycle. Approving an active product
publishes it (the Phase 1 behaviour, where "approved" meant "published"); merchants can unpublish
an approved product to hide it without losing the approval.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, ValidationIssue
from app.services.catalog.serialize import PUBLIC_LIFECYCLE
from app.services.commerce.audit import Actor, record
from app.services.commerce.money import now_utc

LIFECYCLES = ("draft", "active", "archived", "discontinued")


class LifecycleError(ValueError):
    def __init__(self, message: str, code: str = "invalid_state", status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


async def blocking_errors(db: AsyncSession, product_id) -> int:
    return (await db.execute(select(func.count()).select_from(ValidationIssue).where(
        ValidationIssue.product_id == product_id, ValidationIssue.status == "open", ValidationIssue.severity == "error"
    ))).scalar_one()


def _touch(p: Product) -> None:
    p.version += 1
    p.updated_at = now_utc()


def check_approve(p: Product, blocking: int) -> None:
    if p.deleted_at is not None:
        raise LifecycleError("Deleted products cannot be approved; restore it first")
    if blocking:
        raise LifecycleError(f"Resolve {blocking} blocking validation error(s) before approving this product",
                             "has_blocking_issues")


def set_approval(db: AsyncSession, p: Product, status: str, actor: Actor, operation_id=None) -> None:
    old = p.approval_status
    if old == status:
        return
    p.approval_status = status
    if status == "approved":
        p.approved_at = now_utc()
        if p.lifecycle_status in PUBLIC_LIFECYCLE and not p.is_removed:
            if p.publication_status != "published":
                p.published_at = now_utc()
            p.publication_status = "published"
    else:
        p.publication_status = "unpublished"
    _touch(p)
    record(db, p.business_id, actor, f"product.{'approved' if status == 'approved' else 'rejected' if status == 'rejected' else 'sent_to_review'}",
           "product", p.id, f"'{p.name}' {status.replace('_', ' ')} (was {old.replace('_', ' ')})",
           {"approval_status": {"from": old, "to": status}, "publication_status": p.publication_status}, operation_id)


def check_publish(p: Product) -> None:
    if p.approval_status != "approved":
        raise LifecycleError("Approve the product before publishing it")
    if p.lifecycle_status not in PUBLIC_LIFECYCLE:
        raise LifecycleError(f"A {p.lifecycle_status} product cannot be published; set it to active first")
    if p.deleted_at is not None or p.is_removed:
        raise LifecycleError("This product is deleted or no longer on the website")


def set_publication(db: AsyncSession, p: Product, status: str, actor: Actor, operation_id=None) -> None:
    if status == "published":
        check_publish(p)
    if p.publication_status == status:
        return
    old = p.publication_status
    p.publication_status = status
    if status == "published":
        p.published_at = now_utc()
    _touch(p)
    record(db, p.business_id, actor, f"product.{status}", "product", p.id, f"'{p.name}' {status}",
           {"publication_status": {"from": old, "to": status}}, operation_id)


def set_lifecycle(db: AsyncSession, p: Product, status: str, actor: Actor, operation_id=None) -> None:
    if status not in LIFECYCLES:
        raise LifecycleError(f"Lifecycle must be one of {', '.join(LIFECYCLES)}", "invalid_lifecycle", 422)
    if p.deleted_at is not None:
        raise LifecycleError("Restore this product before changing its lifecycle")
    old = p.lifecycle_status
    if old == status:
        return
    p.lifecycle_status = status
    if status == "archived":
        p.archived_at = now_utc()
    elif old == "archived":
        p.archived_at = None
    if status not in PUBLIC_LIFECYCLE:
        p.publication_status = "unpublished"
    _touch(p)
    action = {"archived": "product.archived", "active": "product.restored" if old == "archived" else "product.activated"}.get(
        status, f"product.{status}")
    record(db, p.business_id, actor, action, "product", p.id, f"'{p.name}' lifecycle: {old} → {status}",
           {"lifecycle_status": {"from": old, "to": status}}, operation_id)


def soft_delete(db: AsyncSession, p: Product, actor: Actor, operation_id=None) -> None:
    if p.deleted_at is not None:
        return
    p.deleted_at = now_utc()
    p.publication_status = "unpublished"
    _touch(p)
    record(db, p.business_id, actor, "product.deleted", "product", p.id,
           f"'{p.name}' deleted (kept for history; can be restored)", {}, operation_id)


def restore(db: AsyncSession, p: Product, actor: Actor, operation_id=None) -> None:
    if p.deleted_at is not None:
        p.deleted_at = None
        _touch(p)
        record(db, p.business_id, actor, "product.restored", "product", p.id, f"'{p.name}' restored from deleted", {},
               operation_id)
        return
    if p.lifecycle_status == "archived":
        set_lifecycle(db, p, "active", actor, operation_id)
        return
    raise LifecycleError("Only archived or deleted products can be restored")
