import { describe, expect, it } from "vitest";
import { forecastRows } from "@/components/decision";

describe("forecastRows", () => {
  it("formats money, hides the currency unit and skips nested values", () => {
    const rows = forecastRows(
      {
        monthly_cap_minor: 500000,
        projected_month_minor: 80000,
        currency: "INR",
        probability: 0.923,
        nested: { a: 1 },
      },
      null,
    );
    const labels = rows.map(([l]) => l);
    expect(labels).toEqual(["Monthly cap", "Projected month", "Probability"]);
    expect(rows[0]?.[1]).toContain("5,000");
    expect(rows[2]?.[1]).toBe("0.92");
  });
});
