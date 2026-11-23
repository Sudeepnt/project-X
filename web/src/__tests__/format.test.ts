import { describe, expect, it } from "vitest";
import { niceTicks } from "@/components/charts";
import { formatMoney, formatPercent, sentence, titleCase } from "@/lib/format";

describe("formatMoney", () => {
  it("uses minor units and the currency's exponent", () => {
    expect(formatMoney(549400, "INR")).toContain("5,494");
    expect(formatMoney(1500, "JPY")).toContain("1,500");
    expect(formatMoney(null)).toBe("—");
  });
  it("drops decimals for whole amounts", () => {
    expect(formatMoney(10000, "USD")).not.toContain(".00");
  });
});

describe("text helpers", () => {
  it("formats percentages and labels", () => {
    expect(formatPercent(0.1234)).toBe("12.3%");
    expect(formatPercent(undefined)).toBe("—");
    expect(sentence("needs_review")).toBe("Needs review");
    expect(titleCase("run_and_tell")).toBe("Run And Tell");
  });
});

describe("niceTicks", () => {
  it("lands on round numbers that cover the data", () => {
    expect(niceTicks(5780)).toEqual([0, 2000, 4000, 6000, 8000]);
    expect(niceTicks(3150)).toEqual([0, 1000, 2000, 3000, 4000]);
    expect(niceTicks(18)).toEqual([0, 5, 10, 15, 20]);
  });
  it("never divides by zero on empty data", () => {
    expect(niceTicks(0)).toEqual([0, 1, 2, 3, 4]);
  });
});
