import { describe, expect, it } from "vitest";
import { CHECK_LABELS, OVERALL_TONE, checkDetail } from "@/pages/app/SystemStatus";

// Contract copied from the backend (app/core/health.py run_checks)
const BACKEND_CHECKS = ["database", "redis", "worker", "scheduler"];
const BACKEND_OVERALL = ["ok", "degraded", "unavailable"];

describe("system status contracts", () => {
  it("labels every dependency the backend checks and every overall status", () => {
    expect(Object.keys(CHECK_LABELS).sort()).toEqual([...BACKEND_CHECKS].sort());
    expect(Object.keys(OVERALL_TONE).sort()).toEqual([...BACKEND_OVERALL].sort());
    expect(OVERALL_TONE.unavailable).toBe("danger");
  });
  it("describes real check results, never a made-up success", () => {
    expect(checkDetail("database", { status: "ok", latency_ms: 4.2 })).toBe("4.2 ms");
    expect(checkDetail("worker", { status: "ok", workers: 1 })).toBe("1 worker answered");
    expect(checkDetail("worker", { status: "error", error: "no worker answered" })).toBe("no worker answered");
    expect(checkDetail("scheduler", { status: "ok", age_seconds: 15 })).toBe("Last tick 15s ago");
    expect(checkDetail("redis", { status: "error" })).toBe("Not reachable");
    expect(checkDetail("redis", { status: "not_used", detail: "inline jobs" })).toBe("inline jobs");
  });
});
