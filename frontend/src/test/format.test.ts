import { describe, expect, it } from "vitest";
import { columnLabel, formatAmount, formatDate, formatMoney } from "../format";

describe("format", () => {
  it("formats exact decimal strings without float rounding", () => {
    expect(formatAmount("6887110.91")).toBe("6,887,110.91");
    expect(formatAmount("12345678901234.99")).toBe("12,345,678,901,234.99");
    expect(formatAmount("-52749.39")).toBe("−52,749.39");
    expect(formatAmount("5")).toBe("5.00");
    expect(formatAmount(null)).toBe("unavailable");
  });
  it("prefixes currency", () => {
    expect(formatMoney("1131562.07", "EUR")).toBe("EUR 1,131,562.07");
    expect(formatMoney(null, "EUR")).toBe("unavailable");
  });
  it("formats dates and labels", () => {
    expect(formatDate("2025-03-08")).toBe("8 Mar 2025");
    expect(columnLabel("amount_eur")).toBe("Amount (EUR)");
    expect(columnLabel("total_spend_eur")).toBe("Total spend EUR");
  });
});
