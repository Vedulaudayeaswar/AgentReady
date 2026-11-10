import { describe, expect, it } from "vitest";
import { DELIVERY_TONE, EVENT_TYPES, FEED_FORMATS, FEED_TYPES, PLATFORM_STATUS_TONE, PROFILES } from "@/services/distribution";
import { AVAIL_TONE, CHANNEL_LABELS } from "@/components/app/distribution";

// Contracts copied from the backend (app/services/distribution/*). The UI must not offer anything else.
const BACKEND_EVENTS = ["product.created", "product.updated", "product.published", "product.unpublished",
  "product.price_changed", "product.availability_changed", "product.variant_updated", "product.archived"];
const BACKEND_PROFILES = { agentready: ["json", "jsonl", "csv", "jsonld", "xml"], openai: ["jsonl", "csv"], google: ["xml", "csv"] };
const BACKEND_TYPES = ["full", "incremental", "availability", "pricing", "category", "variant"];
const BACKEND_STATUSES = ["not_configured", "setup_required", "pending_verification", "connected", "syncing", "active", "failed", "disconnected"];
const BACKEND_AVAILABILITY = ["available", "out_of_stock", "preorder", "backorder", "discontinued", "unknown"];

describe("distribution contracts", () => {
  it("offers exactly the webhook event types the backend emits", () => {
    expect([...EVENT_TYPES].sort()).toEqual([...BACKEND_EVENTS].sort());
  });
  it("labels every feed profile, format and type the backend supports", () => {
    expect(Object.keys(PROFILES).sort()).toEqual(Object.keys(BACKEND_PROFILES).sort());
    const formats = new Set(Object.values(BACKEND_PROFILES).flat());
    expect(Object.keys(FEED_FORMATS).sort()).toEqual([...formats].sort());
    expect(Object.keys(FEED_TYPES).sort()).toEqual([...BACKEND_TYPES].sort());
  });
  it("has a tone for every platform connection status (no fake states)", () => {
    expect(Object.keys(PLATFORM_STATUS_TONE).sort()).toEqual([...BACKEND_STATUSES].sort());
    expect(PLATFORM_STATUS_TONE.active).toBe("success");
  });
  it("covers every public availability value and delivery status", () => {
    expect(Object.keys(AVAIL_TONE).sort()).toEqual([...BACKEND_AVAILABILITY].sort());
    expect(Object.keys(DELIVERY_TONE).sort()).toEqual(["delivering", "failed", "pending", "succeeded"]);
  });
  it("names all four distribution channels", () => {
    expect(Object.keys(CHANNEL_LABELS).sort()).toEqual(["agent_api", "feeds", "mcp", "webhooks"]);
  });
});
