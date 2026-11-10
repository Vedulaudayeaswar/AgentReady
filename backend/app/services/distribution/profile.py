"""Merchant distribution profile: who may see what, through which channel."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Category, DistributionProfile
from app.models.distribution import CHANNELS
from app.services.commerce.money import aware, now_utc

CHANNEL_FLAG = {"agent_api": "agent_api_enabled", "mcp": "mcp_enabled", "feeds": "feeds_enabled",
                "webhooks": "webhooks_enabled"}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]
    return s or "merchant"


async def get_profile(db: AsyncSession, business: Business, create: bool = True) -> DistributionProfile | None:
    prof = (await db.execute(select(DistributionProfile).where(DistributionProfile.business_id == business.id))).scalar_one_or_none()
    if prof is None and create:
        base = slugify(business.name)
        slug = base
        while (await db.execute(select(DistributionProfile.id).where(DistributionProfile.slug == slug))).first():
            slug = f"{base}-{secrets.token_hex(3)}"
        from sqlalchemy.exc import IntegrityError

        try:
            # a concurrent request may create the profile (or claim the slug) at the same moment
            async with db.begin_nested():
                prof = DistributionProfile(business_id=business.id, slug=slug, public_name=business.name,
                                           country=business.country, excluded_category_ids=[], hidden_attribute_keys=[])
                db.add(prof)
        except IntegrityError:
            prof = (await db.execute(select(DistributionProfile).where(DistributionProfile.business_id == business.id)
                                     .execution_options(populate_existing=True))).scalar_one_or_none()
            if prof is None:  # slug collision with another merchant: retry once with a random suffix
                async with db.begin_nested():
                    prof = DistributionProfile(business_id=business.id, slug=f"{base}-{secrets.token_hex(3)}",
                                               public_name=business.name, country=business.country,
                                               excluded_category_ids=[], hidden_attribute_keys=[])
                    db.add(prof)
    return prof


def channel_open(prof: DistributionProfile | None, channel: str, at: datetime | None = None) -> tuple[bool, str | None]:
    """Is distribution through ``channel`` allowed right now? Returns (allowed, reason-if-not)."""
    if prof is None or not prof.enabled:
        return False, "Public distribution is disabled by the merchant"
    if prof.expires_at is not None and aware(prof.expires_at) <= (at or now_utc()):
        return False, "The merchant's distribution period has expired"
    flag = CHANNEL_FLAG.get(channel)
    if flag and not getattr(prof, flag):
        return False, f"The merchant has disabled the {channel.replace('_', ' ')} channel"
    return True, None


async def excluded_category_tree(db: AsyncSession, prof: DistributionProfile) -> set[uuid.UUID]:
    """Excluded categories plus all their descendants."""
    roots = set()
    for c in prof.excluded_category_ids or []:
        try:
            roots.add(uuid.UUID(str(c)))
        except ValueError:
            continue
    if not roots:
        return set()
    cats = (await db.execute(select(Category.id, Category.parent_id).where(Category.business_id == prof.business_id))).all()
    out, frontier = set(roots), set(roots)
    while frontier:
        frontier = {cid for cid, parent in cats if parent in frontier} - out
        out |= frontier
    return out


@dataclass
class MerchantScope:
    """A merchant visible to the current consumer, with everything the serializer needs."""

    business: Business
    profile: DistributionProfile
    excluded_categories: set[uuid.UUID] = field(default_factory=set)
    category_paths: dict[uuid.UUID, str] = field(default_factory=dict)

    @property
    def business_id(self) -> uuid.UUID:
        return self.business.id

    def merchant_ref(self) -> dict:
        p = self.profile
        return {"id": p.slug, "name": p.public_name or self.business.name, "url": p.public_url, "country": p.country}


async def merchant_scope(db: AsyncSession, business: Business, prof: DistributionProfile) -> MerchantScope:
    cats = (await db.execute(select(Category.id, Category.path).where(Category.business_id == business.id,
                                                                      Category.archived_at.is_(None)))).all()
    return MerchantScope(business, prof, await excluded_category_tree(db, prof), {cid: path for cid, path in cats})


def profile_dict(p: DistributionProfile) -> dict:
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {
        "enabled": p.enabled, "agent_api_enabled": p.agent_api_enabled, "mcp_enabled": p.mcp_enabled,
        "feeds_enabled": p.feeds_enabled, "webhooks_enabled": p.webhooks_enabled, "public_network": p.public_network,
        "product_scope": p.product_scope, "excluded_category_ids": [str(c) for c in p.excluded_category_ids or []],
        "show_prices": p.show_prices, "show_availability": p.show_availability,
        "public_attribute_keys": p.public_attribute_keys, "hidden_attribute_keys": p.hidden_attribute_keys or [],
        "expires_at": iso(p.expires_at), "slug": p.slug, "public_name": p.public_name, "public_url": p.public_url,
        "country": p.country, "support_email": p.support_email, "return_policy_url": p.return_policy_url,
        "privacy_policy_url": p.privacy_policy_url, "terms_url": p.terms_url, "checkout_enabled": p.checkout_enabled,
        "channels": list(CHANNELS), "updated_at": iso(p.updated_at),
    }
