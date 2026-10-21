import { describe, expect, it } from "vitest";

import type { Target } from "./config";
import { fakeDeps } from "./fakes.test-support";
import { checkAll, judge, RECHECK_AFTER_MS, statusOf } from "./probe";

const site = (url: string, expect: "ok" | "answer" = "ok"): Target => ({ project: "eva", projectName: "Eva", url, expect, part: null });

describe("judge (same rules as core/sdk/uptime.py)", () => {
  it.each([
    ["ok", 200, true, "ok"],
    ["ok", 204, true, "ok"],
    ["ok", 301, true, "redirect"],
    ["ok", 399, true, "redirect"],
    ["ok", 404, false, "not_found"],
    ["ok", 401, false, "refused"],
    ["ok", 199, false, "refused"],
    ["ok", 500, false, "server_error"],
    ["answer", 404, true, "answered"],
    ["answer", 403, true, "answered"],
    ["answer", 503, false, "server_error"],
    ["ok", null, false, "no_answer"],
    ["answer", null, false, "no_answer"],
  ] as const)("%s %s -> up=%s (%s)", (rule, status, up, reason) => {
    expect(judge(rule, status)).toEqual({ up, reason });
  });
});

describe("statusOf", () => {
  it("asks once, status only, without following redirects", async () => {
    const { deps, calls } = fakeDeps({ "https://a.example.com": [302] });
    let seen: RequestInit | undefined;
    const spy = { ...deps, fetch: async (url: string, init: RequestInit) => ((seen = init), deps.fetch(url, init)) };
    expect(await statusOf("https://a.example.com", spy)).toBe(302);
    expect(calls).toEqual(["https://a.example.com"]);
    expect(seen?.method).toBe("GET");
    expect(seen?.redirect).toBe("manual");
    expect(seen?.signal).toBeInstanceOf(AbortSignal);
  });

  it("no answer (network error or timeout) is null", async () => {
    const { deps } = fakeDeps({ "https://a.example.com": [null] });
    expect(await statusOf("https://a.example.com", deps)).toBeNull();
  });
});

describe("checkAll", () => {
  it("all up: one check each and no waiting", async () => {
    const { deps, calls, waits } = fakeDeps({ "https://a.example.com": [200], "https://b.example.com": [301] });
    const result = await checkAll([site("https://a.example.com"), site("https://b.example.com")], deps);
    expect(result.map((r) => r.up)).toEqual([true, true]);
    expect(calls).toHaveLength(2);
    expect(waits).toEqual([]);
  });

  it("a blip that recovers on the re-check counts as up", async () => {
    const { deps, waits } = fakeDeps({ "https://a.example.com": [503, 200] });
    const [only] = await checkAll([site("https://a.example.com")], deps);
    expect(only).toMatchObject({ up: true, reason: "ok" });
    expect(waits).toEqual([RECHECK_AFTER_MS]);
  });

  it("down only when the re-check fails too; only failures are re-checked", async () => {
    const { deps, calls } = fakeDeps({ "https://a.example.com": [404, 404], "https://b.example.com": [200] });
    const result = await checkAll([site("https://a.example.com"), site("https://b.example.com")], deps);
    expect(result.map((r) => [r.up, r.reason])).toEqual([[false, "not_found"], [true, "ok"]]);
    expect(calls.filter((u) => u === "https://b.example.com")).toHaveLength(1);
    expect(calls.filter((u) => u === "https://a.example.com")).toHaveLength(2);
  });
});
