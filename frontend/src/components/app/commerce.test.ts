import { describe, expect, it } from "vitest";
import { BULK_OPS, display, money } from "@/components/app/commerce";

// Operation keys the backend accepts (app/services/commerce/bulk.py OPERATIONS) — the UI must not offer anything else.
const BACKEND_OPERATIONS = [
  "approve", "send_to_review", "reject", "publish", "unpublish", "archive", "restore", "activate", "discontinue", "delete",
  "assign_category", "set_brand", "set_product_type", "add_attribute", "remove_attribute", "price_adjust", "set_sale",
  "clear_sale", "schedule_price", "inventory_adjust", "inventory_settings",
];

describe("money", () => {
  it("formats decimal strings from the API without changing the amount", () => {
    expect(money("849.00", "INR")).toMatch(/849(\.00)?/);
    expect(money("0.10", "USD")).toMatch(/0\.10/);
  });
  it("shows a dash for missing prices and never invents a currency", () => {
    expect(money(null, "INR")).toBe("—");
    expect(money("", "INR")).toBe("—");
    expect(money("12.5", null)).toBe("12.50");
  });
  it("falls back gracefully for unknown currency codes", () => {
    expect(money("5", "ZZZ")).toMatch(/5\.00/);
  });
});

describe("display", () => {
  it("renders conflict values of every shape", () => {
    expect(display(null)).toBe("—");
    expect(display("475.00")).toBe("475.00");
    expect(display(["a.jpg", "b.jpg"])).toBe("a.jpg, b.jpg");
    expect(display({ voltage: ["12", "V"] })).toBe("Voltage: 12, V");
  });
});

describe("bulk operations", () => {
  it("only offers operations the backend implements (no buttons without a backend)", () => {
    expect(BULK_OPS.map((o) => o.key).sort()).toEqual([...BACKEND_OPERATIONS].sort());
  });
});
