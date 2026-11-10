from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.core.errors import AppError, not_found
from app.core.ratelimit import rate_limit
from app.db.session import get_db
from app.models import Website
from app.schemas.api import UrlCheckIn, WebsiteIn, WebsiteUpdate
from app.services.crawler.fetcher import BlockedURL, SafeFetcher
from app.services.crawler.urls import InvalidURL, validate_public_url_syntax

router = APIRouter(prefix="/websites", tags=["websites"])


def website_dict(w: Website) -> dict:
    return {
        "id": str(w.id), "url": w.url, "normalized_url": w.normalized_url, "domain": w.domain, "name": w.name,
        "max_pages": w.max_pages, "max_depth": w.max_depth, "use_sitemap": w.use_sitemap, "render_js": w.render_js,
        "last_scan_at": w.last_scan_at.isoformat() if w.last_scan_at else None,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


async def _validate(url: str, network: bool = True) -> str:
    try:
        normalized = validate_public_url_syntax(url)
    except InvalidURL as e:
        raise AppError(422, "invalid_url", str(e))
    if network:
        fetcher = SafeFetcher()
        try:
            await fetcher.check_url(normalized)
        except BlockedURL as e:
            raise AppError(422, "blocked_url", f"This URL cannot be scanned: {e}")
        finally:
            await fetcher.aclose()
    return normalized


async def get_owned_website(db: AsyncSession, ctx: AuthContext, website_id: str | uuid.UUID) -> Website:
    try:
        wid = uuid.UUID(str(website_id))
    except ValueError:
        raise not_found("Website")
    w = (await db.execute(select(Website).where(Website.id == wid, Website.business_id == ctx.business_id))).scalar_one_or_none()
    if w is None:
        raise not_found("Website")
    return w


@router.post("/validate-url", dependencies=[Depends(rate_limit("validate_url", limit=20))])
async def validate_url(body: UrlCheckIn, ctx: AuthContext = Depends(current_auth)):
    """Syntax + DNS/SSRF validation, plus a reachability probe of the homepage."""
    normalized = await _validate(body.url)
    fetcher = SafeFetcher()
    try:
        res = await fetcher.fetch(normalized)
        robots = await fetcher.fetch(f"{urlsplit(normalized).scheme}://{urlsplit(normalized).netloc}/robots.txt")
    finally:
        await fetcher.aclose()
    return {
        "normalized_url": normalized,
        "reachable": res.ok,
        "status_code": res.status_code,
        "error": res.error,
        "final_url": res.redirects[-1] if res.redirects else normalized,
        "robots_txt_found": robots.ok,
    }


@router.post("", status_code=201)
async def create_website(body: WebsiteIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    normalized = await _validate(body.url)
    existing = (await db.execute(select(Website).where(
        Website.business_id == ctx.business_id, Website.normalized_url == normalized))).scalar_one_or_none()
    if existing:
        raise AppError(409, "website_exists", "This website is already connected", {"website_id": str(existing.id)})
    w = Website(business_id=ctx.business_id, url=body.url.strip(), normalized_url=normalized,
                domain=urlsplit(normalized).hostname or "", name=body.name, max_pages=body.max_pages,
                max_depth=body.max_depth, use_sitemap=body.use_sitemap, render_js=body.render_js)
    db.add(w)
    await db.commit()
    return website_dict(w)


@router.get("")
async def list_websites(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Website).where(Website.business_id == ctx.business_id)
                             .order_by(Website.created_at.desc()))).scalars().all()
    return {"items": [website_dict(w) for w in rows]}


@router.get("/{website_id}")
async def get_website(website_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    return website_dict(await get_owned_website(db, ctx, website_id))


@router.patch("/{website_id}")
async def update_website(website_id: str, body: WebsiteUpdate, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    w = await get_owned_website(db, ctx, website_id)
    for f in body.model_fields_set:
        setattr(w, f, getattr(body, f))
    await db.commit()
    return website_dict(w)


@router.delete("/{website_id}", status_code=204)
async def delete_website(website_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    w = await get_owned_website(db, ctx, website_id)
    await db.delete(w)
    await db.commit()
    return Response(status_code=204)
