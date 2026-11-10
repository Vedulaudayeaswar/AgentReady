"""Change capture for distribution: one hook sees every product change, whatever code path made it.

A SQLAlchemy ``before_flush`` listener inspects new/changed/deleted products, variants and attributes and writes
``OutboxEvent`` rows *in the same transaction* (transactional outbox: an event exists if and only if the change
was committed). The webhook dispatcher fans the outbox out to subscriptions later. Code that changes rows with
core UPDATE statements (inventory's optimistic-concurrency update) calls ``note_event`` explicitly.

After a commit, the catalog generation of every affected merchant is bumped, which invalidates cached public
responses (see ``cache``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models import Category, DistributionProfile, OutboxEvent, Product, ProductAttribute, ProductVariant
from app.services.distribution import cache

EVENT_TYPES = ("product.created", "product.updated", "product.published", "product.unpublished",
               "product.price_changed", "product.availability_changed", "product.variant_updated", "product.archived")
PUBLIC_LIFECYCLE = ("active", "discontinued")
PUBLIC_FIELDS = {"name", "description", "brand", "manufacturer", "sku", "gtin", "mpn", "category", "category_id",
                 "product_type", "product_url", "source_url", "images", "weight", "dimensions", "material", "shipping",
                 "public_description", "public_images", "public_attribute_keys", "distribution_channels"}
PRICE_FIELDS = {"price_amount", "sale_price_amount", "sale_starts_at", "sale_ends_at", "currency", "tax_included"}
AVAILABILITY_FIELDS = {"availability", "stock_status", "track_inventory", "backorder_policy"}
VISIBILITY_FIELDS = ("approval_status", "publication_status", "lifecycle_status", "deleted_at", "is_removed",
                     "distribution_enabled")
_INFO_EVENTS = "ar_dist_events"
_INFO_BUMP = "ar_dist_bump"
GLOBAL = "*"


def _visible(v: dict) -> bool:
    return (v["approval_status"] == "approved" and v["publication_status"] == "published"
            and v["lifecycle_status"] in PUBLIC_LIFECYCLE and v["deleted_at"] is None and not v["is_removed"]
            and v["distribution_enabled"] is not False)


def _changed(obj, names) -> set[str]:
    st = inspect(obj)
    return {n for n in names if n in st.attrs and st.attrs[n].history.has_changes()}


def _before(obj, name):
    h = inspect(obj).attrs[name].history
    if h.has_changes():
        return h.deleted[0] if h.deleted else None
    return getattr(obj, name)


def _emit(session: Session, business_id, event_type: str, product_id, variant_id=None, fields=()) -> None:
    seen: set = session.info.setdefault(_INFO_EVENTS, set())
    key = (event_type, product_id, variant_id)
    if business_id is None or product_id is None or key in seen:
        return
    seen.add(key)
    session.add(OutboxEvent(business_id=business_id, event_type=event_type, product_id=product_id,
                            variant_id=variant_id, changed_fields=sorted(fields)))


def _bump(session: Session, business_id) -> None:
    session.info.setdefault(_INFO_BUMP, set()).add(str(business_id) if business_id else GLOBAL)


def note_event(session, business_id, product_id, event_type: str, variant_id=None, fields=()) -> None:
    """Record an event for a change made outside the ORM unit of work (core UPDATE)."""
    s = getattr(session, "sync_session", session)
    _emit(s, business_id, event_type, product_id, variant_id, fields)
    _bump(s, business_id)


def _product_for(session: Session, product_id, child=None) -> Product | None:
    """Parent product of a variant/attribute; children added through the relationship have no product_id yet."""
    from sqlalchemy.orm.util import identity_key

    if product_id is not None:
        return session.identity_map.get(identity_key(Product, product_id))
    if child is not None:
        attr = "variants" if isinstance(child, ProductVariant) else "attributes"
        for obj in list(session.new) + list(session.identity_map.values()):
            if isinstance(obj, Product) and attr in obj.__dict__ and child in obj.__dict__[attr]:
                return obj
    return None


@event.listens_for(Session, "before_flush")
def _capture(session: Session, flush_context, instances) -> None:
    for obj in list(session.new):
        if isinstance(obj, Product):
            if obj.id is None:
                obj.id = uuid.uuid4()
            _emit(session, obj.business_id, "product.created", obj.id)
            if _visible({f: getattr(obj, f) for f in VISIBILITY_FIELDS}):
                _emit(session, obj.business_id, "product.published", obj.id)
            _bump(session, obj.business_id)
        elif isinstance(obj, ProductVariant):
            if obj.id is None:
                obj.id = uuid.uuid4()
            parent = _product_for(session, obj.product_id, obj)
            bid = obj.business_id or (parent.business_id if parent else None)
            pid = obj.product_id or (parent.id if parent else None)
            if parent is None or parent not in session.new:
                _emit(session, bid, "product.variant_updated", pid, obj.id, ["created"])
            _bump(session, bid)
        elif isinstance(obj, ProductAttribute):
            parent = _product_for(session, obj.product_id, obj)
            if parent is not None and parent not in session.new:
                _emit(session, parent.business_id, "product.updated", parent.id, None, ["attributes"])
            _bump(session, parent.business_id if parent else None)
        elif isinstance(obj, (DistributionProfile, Category)):
            _bump(session, obj.business_id)
    for obj in list(session.dirty):
        if not session.is_modified(obj, include_collections=False):
            continue
        if isinstance(obj, Product):
            changed = _changed(obj, PUBLIC_FIELDS | PRICE_FIELDS | AVAILABILITY_FIELDS | set(VISIBILITY_FIELDS))
            if not changed:
                continue
            _bump(session, obj.business_id)
            before = _visible({f: _before(obj, f) for f in VISIBILITY_FIELDS})
            after = _visible({f: getattr(obj, f) for f in VISIBILITY_FIELDS})
            if not before and after:
                _emit(session, obj.business_id, "product.published", obj.id, None, changed)
            elif before and not after:
                kind = "product.archived" if obj.lifecycle_status == "archived" else "product.unpublished"
                _emit(session, obj.business_id, kind, obj.id, None, changed)
            elif after:
                if changed & PRICE_FIELDS:
                    _emit(session, obj.business_id, "product.price_changed", obj.id, None, changed & PRICE_FIELDS)
                if changed & AVAILABILITY_FIELDS:
                    _emit(session, obj.business_id, "product.availability_changed", obj.id, None, changed & AVAILABILITY_FIELDS)
                if changed & PUBLIC_FIELDS:
                    _emit(session, obj.business_id, "product.updated", obj.id, None, changed & PUBLIC_FIELDS)
        elif isinstance(obj, ProductVariant):
            st = inspect(obj)
            changed = {a.key for a in st.attrs if a.history.has_changes()} - {"updated_at"}
            if changed:
                _emit(session, obj.business_id, "product.variant_updated", obj.product_id, obj.id, changed)
                _bump(session, obj.business_id)
        elif isinstance(obj, ProductAttribute):
            parent = _product_for(session, obj.product_id, obj)
            if parent is not None:
                _emit(session, parent.business_id, "product.updated", parent.id, None, ["attributes"])
            _bump(session, parent.business_id if parent else None)
        elif isinstance(obj, (DistributionProfile, Category)):
            _bump(session, obj.business_id)
    for obj in list(session.deleted):
        if isinstance(obj, ProductVariant):
            _emit(session, obj.business_id, "product.variant_updated", obj.product_id, None, ["deleted"])
            _bump(session, obj.business_id)
        elif isinstance(obj, ProductAttribute):
            parent = _product_for(session, obj.product_id, obj)
            if parent is not None:
                _emit(session, parent.business_id, "product.updated", parent.id, None, ["attributes"])
            _bump(session, parent.business_id if parent else None)
        elif isinstance(obj, Product):
            _bump(session, obj.business_id)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    ids = session.info.pop(_INFO_BUMP, set())
    session.info.pop(_INFO_EVENTS, None)
    if ids:
        cache.bump(ids)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_INFO_BUMP, None)
    session.info.pop(_INFO_EVENTS, None)
