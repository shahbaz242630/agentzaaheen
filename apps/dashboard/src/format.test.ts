import { describe, expect, it } from "vitest";

import { ago, explainUnknown, formatValue, freshnessLabel, minorDigits } from "./format";

const NOW = new Date("2026-10-18T12:00:00Z");

describe("formatValue", () => {
  it("formats money from minor units", () => {
    expect(formatValue({ kind: "money", minor: 124_000, currency: "USD" }, "en-US")).toBe("$1,240");
    expect(formatValue({ kind: "money", minor: 5_850, currency: "USD" }, "en-US")).toBe("$58.50");
  });

  it("knows currencies without minor units", () => {
    expect(minorDigits("JPY")).toBe(0);
    expect(formatValue({ kind: "money", minor: 1_500, currency: "JPY" }, "en-US")).toBe("¥1,500");
  });

  it("formats counts, percents and unknowns", () => {
    expect(formatValue({ kind: "count", value: 1234 }, "en-US")).toBe("1,234");
    expect(formatValue({ kind: "percent", basis_points: 9_995 })).toBe("99.95%");
    expect(formatValue({ kind: "unknown", reason: "source.not_connected" })).toBe("unknown");
  });
});

describe("explainUnknown", () => {
  it("explains known reasons and falls back for others", () => {
    expect(explainUnknown("source.not_connected")).toBe("Not connected yet");
    expect(explainUnknown("net.timeout")).toBe("Cannot be worked out right now");
  });
});

describe("ago", () => {
  it.each([
    ["2026-10-18T11:59:30Z", "just now"],
    ["2026-10-18T11:48:00Z", "12 min ago"],
    ["2026-10-18T09:00:00Z", "3 h ago"],
    ["2026-10-17T10:00:00Z", "yesterday"],
    ["2026-10-14T12:00:00Z", "4 days ago"],
    ["2026-10-18T12:05:00Z", "just now"],
  ])("%s is %s", (iso, words) => {
    expect(ago(iso, NOW)).toBe(words);
  });
});

describe("freshnessLabel", () => {
  it("never shows a stale number as current", () => {
    expect(freshnessLabel("fresh", "2026-10-18T11:48:00Z", NOW)).toBe("Updated 12 min ago");
    expect(freshnessLabel("fresh", null, NOW)).toBe("Up to date");
    expect(freshnessLabel("stale", "2026-10-17T11:48:00Z", NOW)).toMatch(/numbers hidden/);
    expect(freshnessLabel("alert", "2026-10-10T11:48:00Z", NOW)).toMatch(/numbers hidden/);
    expect(freshnessLabel("never", null, NOW)).toBe("Not connected yet");
  });
});
