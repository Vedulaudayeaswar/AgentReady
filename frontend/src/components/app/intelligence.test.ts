import { describe, expect, it } from "vitest";
import { LEVEL_THRESHOLDS, LEVEL_TONE, PRIORITY_TONE, REPORT_KINDS, SEVERITY_TONE, levelFor } from "@/services/intelligence";

// Contracts copied from the backend (app/services/intelligence/*).
const BACKEND_LEVELS: [string, number][] = [["excellent", 85], ["good", 70], ["needs_improvement", 50], ["critical", 0]];
const BACKEND_REPORTS = ["catalog_quality", "missing_attributes", "product_completeness", "distribution_activity", "search_performance",
  "freshness", "pricing_history", "inventory_health", "sync_conflicts", "recommendations"];

describe("intelligence contracts", () => {
  it("uses the backend's quality level thresholds", () => {
    expect(LEVEL_THRESHOLDS).toEqual(BACKEND_LEVELS);
    expect(levelFor(85)).toBe("excellent");
    expect(levelFor(84.9)).toBe("good");
    expect(levelFor(50)).toBe("needs_improvement");
    expect(levelFor(12)).toBe("critical");
  });
  it("offers exactly the report kinds the backend can generate", () => {
    expect([...REPORT_KINDS].sort()).toEqual([...BACKEND_REPORTS].sort());
  });
  it("has a tone for every level, priority and anomaly severity", () => {
    expect(Object.keys(LEVEL_TONE).sort()).toEqual(["critical", "excellent", "good", "needs_improvement"]);
    expect(Object.keys(PRIORITY_TONE).sort()).toEqual(["critical", "high", "low", "medium"]);
    expect(PRIORITY_TONE.critical).toBe("danger");
    expect(Object.keys(SEVERITY_TONE).sort()).toEqual(["high", "low", "medium"]);
  });
});
