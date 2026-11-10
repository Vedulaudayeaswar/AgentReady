"""AI commerce platform channels.

Each provider has an adapter implementing ``PlatformProvider``. Platform-specific field mapping lives in the
feed profiles (``feeds.openai_rows`` / ``feeds.google_rows``), never in the canonical product model.

What the official documentation says (researched November 2026 — see docs/phase3-platforms.md):

* ChatGPT (OpenAI Product Feed spec, developers.openai.com/commerce): onboarding is for *approved partners*
  (apply at chatgpt.com/merchants); feeds are pushed over SFTP (credentials issued during onboarding), at least
  daily; US-focused; checkout (Agentic Commerce Protocol) is a separately enabled integration. AgentReady can
  generate and validate the feed; submission is BLOCKED until OpenAI approves the merchant and issues SFTP access.
* Google (Merchant Center + Universal Commerce Protocol, ucp.dev / developers.google.com/merchant/ucp): requires a
  Merchant Center account with approved free listings; UCP checkout is early access for select US merchants via
  an interest form. Catalog updates go through the Merchant API with Google OAuth. AgentReady generates and
  validates a Merchant Center feed; API submission is BLOCKED (needs Google OAuth credentials / approval).
* Perplexity: its official developer documentation (docs.perplexity.ai) lists no merchant or catalog submission
  API, and no official merchant feed specification was found. Marked BLOCKED; no submission is attempted.
* Independent MCP clients: fully supported today through AgentReady's own MCP server. Status is derived from real
  MCP traffic.

No status is ever set to connected/active without evidence: platform statuses move to ``pending_verification``
only when the merchant reports having applied, and only AgentReady-observed traffic makes the MCP channel active.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ChannelSyncRun, DistributionChannel, DistributionEvent, DistributionProfile
from app.services.commerce.money import aware, now_utc

STATUSES = ("not_configured", "setup_required", "pending_verification", "connected", "syncing", "active", "failed",
            "disconnected")


@dataclass
class Capabilities:
    discovery: bool
    feed_formats: list[str]
    delivery: str
    checkout: str
    regions: str
    submission_api: str
    live_status: str  # available | blocked | not_available
    requirements: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


class PlatformProvider:
    key = "base"
    name = "Base"
    feed_profile: str | None = None
    feed_format: str | None = None
    required_profile_fields: tuple[str, ...] = ()

    def capabilities(self) -> Capabilities:  # pragma: no cover - abstract
        raise NotImplementedError

    def onboarding_steps(self, ch: DistributionChannel, prof: DistributionProfile | None) -> list[dict]:
        return []

    def missing_setup(self, prof: DistributionProfile | None) -> list[str]:
        if prof is None or not prof.enabled:
            return ["Enable public distribution"]
        return [f for f in self.required_profile_fields if not getattr(prof, f, None)]

    async def connection_status(self, db: AsyncSession, ch: DistributionChannel, prof: DistributionProfile | None) -> str:
        if not ch.config.get("enabled"):
            return "not_configured"
        if self.missing_setup(prof):
            return "setup_required"
        if ch.last_error and ch.status == "failed":
            return "failed"
        return "pending_verification" if ch.application_submitted_at else "setup_required"

    async def submit_catalog(self, db: AsyncSession, ch: DistributionChannel) -> tuple[str, str]:
        cap = self.capabilities()
        return "blocked", cap.blocked_reason or "Catalog submission is not available for this platform"

    async def handle_webhook(self, payload: dict) -> None:
        raise NotImplementedError("This platform does not send webhooks to AgentReady")


class OpenAIChatGPT(PlatformProvider):
    key = "openai_chatgpt"
    name = "ChatGPT (OpenAI)"
    feed_profile, feed_format = "openai", "jsonl"
    required_profile_fields = ("public_name", "public_url")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            discovery=True, feed_formats=["jsonl.gz", "csv.gz", "tsv.gz", "parquet"], delivery="SFTP push (credentials issued by OpenAI after approval)",
            checkout="Agentic Commerce Protocol — separately enabled; merchant implements checkout endpoints; merchant of record",
            regions="United States (standard upload)", submission_api="SFTP (no public self-serve API)", live_status="blocked",
            requirements=["Apply at chatgpt.com/merchants and be approved as a partner",
                          "Required fields per item: item_id, title, description, url, brand, seller_name, image_url, availability, price",
                          "Price format '79.99 USD'; availability in_stock|out_of_stock|pre_order|backorder|unknown",
                          "Full snapshot at least daily; start with a ~100-item sample",
                          "Checkout additionally needs seller_privacy_policy and seller_tos"],
            docs=["https://developers.openai.com/commerce", "https://developers.openai.com/commerce/specs/file-upload/products",
                  "https://chatgpt.com/merchants"],
            blocked_reason="OpenAI only accepts feeds from approved partners over SFTP credentials it issues during "
                           "onboarding. Download the validated feed from Product Feeds and upload it once approved.")

    def onboarding_steps(self, ch, prof):
        return [
            {"step": "Enable public distribution and fill in your public store name and website", "done": not self.missing_setup(prof)},
            {"step": "Create an OpenAI-profile feed and fix validation errors", "done": bool(ch.feed_id and ch.last_validation.get("valid"))},
            {"step": "Apply at chatgpt.com/merchants", "done": ch.application_submitted_at is not None},
            {"step": "Receive approval and SFTP credentials from OpenAI", "done": False, "blocked": True},
        ]


class GoogleUCP(PlatformProvider):
    key = "google_ucp"
    name = "Google (Merchant Center / UCP)"
    feed_profile, feed_format = "google", "xml"
    required_profile_fields = ("public_name", "public_url", "return_policy_url", "support_email")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            discovery=True, feed_formats=["xml (RSS 2.0, g: namespace)", "tsv/csv"], delivery="Merchant Center data source or Merchant API",
            checkout="Universal Commerce Protocol native checkout — early access for select US merchants; opt-in per item "
                     "with native_commerce(checkout_eligibility); business profile at /.well-known/ucp",
            regions="United States first (Australia and Canada announced)", submission_api="Merchant API (Google OAuth)",
            live_status="blocked",
            requirements=["Merchant Center account in good standing with approved free listings",
                          "Return policy and a customer support contact configured in Merchant Center",
                          "UCP: submit the interest form and implement the UCP checkout REST endpoints",
                          "Required feed attributes: id, title, description, link, image_link, availability, price (brand/gtin for most new products)"],
            docs=["https://developers.google.com/merchant/ucp", "https://ucp.dev", "https://developers.google.com/merchant/api",
                  "https://support.google.com/merchants/contact/ucp_integration_interest"],
            blocked_reason="Submitting through the Merchant API needs a Google Cloud OAuth client and the merchant's "
                           "Merchant Center authorization (not configured). UCP checkout needs Google approval. The "
                           "validated feed can be added to Merchant Center as a scheduled-fetch data source.")

    def onboarding_steps(self, ch, prof):
        return [
            {"step": "Public store name, website, return policy URL and support email", "done": not self.missing_setup(prof)},
            {"step": "Create a Google-profile feed and fix validation errors", "done": bool(ch.feed_id and ch.last_validation.get("valid"))},
            {"step": "Add the feed URL to Merchant Center as a data source (scheduled fetch)", "done": ch.application_submitted_at is not None},
            {"step": "Merchant API / UCP access approved by Google", "done": False, "blocked": True},
        ]


class Perplexity(PlatformProvider):
    key = "perplexity"
    name = "Perplexity"
    feed_profile, feed_format = None, None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            discovery=False, feed_formats=[], delivery="No documented merchant submission channel", checkout="Not documented",
            regions="Unknown", submission_api="None documented", live_status="not_available",
            requirements=["No official merchant program specification or catalog API was found in Perplexity's documentation "
                          "(docs.perplexity.ai lists Search, Agent, Decisions, Orchestrator and Embeddings APIs only)"],
            docs=["https://docs.perplexity.ai", "https://www.perplexity.ai/merchants"],
            blocked_reason="Perplexity does not document a merchant catalog submission API. Product pages with "
                           "schema.org markup remain crawlable by any search system, but that does not guarantee inclusion.")

    async def connection_status(self, db, ch, prof):
        return "not_configured" if not ch.config.get("enabled") else "setup_required"


class MCPClients(PlatformProvider):
    key = "mcp_clients"
    name = "MCP-compatible AI clients"

    def capabilities(self) -> Capabilities:
        base = get_settings().api_base_url.rstrip("/")
        return Capabilities(
            discovery=True, feed_formats=["MCP tools (JSON)"], delivery=f"Streamable HTTP MCP endpoint {base}/api/v3/mcp",
            checkout="Not exposed as an MCP tool (read-only tools only)", regions="Any",
            submission_api="Not needed: clients connect to AgentReady directly", live_status="available",
            requirements=["Enable public distribution and the MCP channel",
                          "Give the client an API key with the agent:read scope, or join the public network"],
            docs=["https://modelcontextprotocol.io/specification", f"{base}/api/v3/agents/capabilities"])

    def missing_setup(self, prof):
        if prof is None or not prof.enabled:
            return ["Enable public distribution"]
        return [] if prof.mcp_enabled else ["Enable the MCP channel"]

    async def connection_status(self, db, ch, prof):
        if self.missing_setup(prof):
            return "setup_required" if ch.config.get("enabled") or (prof and prof.enabled) else "not_configured"
        recent = (await db.execute(select(func.count()).select_from(DistributionEvent).where(
            DistributionEvent.business_id == ch.business_id, DistributionEvent.channel == "mcp",
            DistributionEvent.created_at >= now_utc() - timedelta(days=7)))).scalar_one()
        return "active" if recent else "connected"


PROVIDERS: dict[str, PlatformProvider] = {p.key: p for p in (OpenAIChatGPT(), GoogleUCP(), Perplexity(), MCPClients())}


async def get_channel(db: AsyncSession, business_id, provider: str) -> DistributionChannel:
    ch = (await db.execute(select(DistributionChannel).where(DistributionChannel.business_id == business_id,
                                                             DistributionChannel.provider == provider))).scalar_one_or_none()
    if ch is None:
        ch = DistributionChannel(business_id=business_id, provider=provider, status="not_configured", config={},
                                 last_validation={})
        db.add(ch)
        await db.flush()
    return ch


async def channel_dict(db: AsyncSession, ch: DistributionChannel, prof: DistributionProfile | None) -> dict:
    prov = PROVIDERS[ch.provider]
    status = await prov.connection_status(db, ch, prof)
    if status != ch.status:
        ch.status = status
    cap = prov.capabilities()
    runs = (await db.execute(select(ChannelSyncRun).where(ChannelSyncRun.channel_id == ch.id)
                             .order_by(ChannelSyncRun.created_at.desc()).limit(10))).scalars().all()
    iso = lambda d: aware(d).isoformat() if d else None  # noqa: E731
    return {"id": str(ch.id), "provider": ch.provider, "name": prov.name, "status": status,
            "enabled": bool(ch.config.get("enabled")), "config": ch.config, "feed_id": str(ch.feed_id) if ch.feed_id else None,
            "feed_profile": prov.feed_profile, "feed_format": prov.feed_format,
            "missing_setup": prov.missing_setup(prof), "onboarding": prov.onboarding_steps(ch, prof),
            "application_submitted_at": iso(ch.application_submitted_at),
            "last_validated_at": iso(ch.last_validated_at), "last_validation": ch.last_validation or {},
            "last_sync_at": iso(ch.last_sync_at), "last_error": ch.last_error,
            "capabilities": cap.__dict__,
            "history": [{"id": str(r.id), "action": r.action, "status": r.status, "products": r.products, "valid": r.valid,
                         "invalid": r.invalid, "message": r.message, "at": iso(r.created_at)} for r in runs]}
