import { describe, expect, it } from "vitest";

import { callsNeeded, MAX_CALLS, readSettings, readTargets, SettingsError } from "./config";

const KEY = "abcdefghijklmnopqrstuv";
const PING = "https://hc-ping.com/12345678-1234-1234-1234-123456789abc";
const ONE = JSON.stringify({ eva: { name: "Eva", sites: [{ url: "https://api.example.com/health", expect: "ok" }] } });

describe("settings", () => {
  it("reads valid settings, trimming stray whitespace", () => {
    const settings = readSettings({ TARGETS: ONE, HEALTHCHECKS_PING_KEY: ` ${KEY}\n`, HEALTHCHECKS_PING_URL: `${PING} ` });
    expect(settings.pingKey).toBe(KEY);
    expect(settings.pingUrl).toBe(PING);
    expect(settings.targets).toEqual([
      { project: "eva", projectName: "Eva", url: "https://api.example.com/health", expect: "ok", part: null, status: null },
    ]);
  });

  it("reads a part's name when one is given", () => {
    const raw = JSON.stringify({ eva: { name: "Eva", sites: [{ url: "https://a.example.com", expect: "ok", part: "sign-in" }] } });
    expect(readTargets(raw)[0]?.part).toBe("sign-in");
  });

  it("reads an exact status with its rule, including the lowest and highest codes", () => {
    for (const code of [100, 405, 499]) {
      const raw = JSON.stringify({ zaaheen: { name: "Zaaheen", sites: [{ url: "https://api.example.com/v1/x", expect: "status", status: code }] } });
      expect(readTargets(raw)[0]).toMatchObject({ expect: "status", status: code });
    }
  });

  it.each([
    [{ TARGETS: ONE, HEALTHCHECKS_PING_URL: PING }, "HEALTHCHECKS_PING_KEY"],
    [{ TARGETS: ONE, HEALTHCHECKS_PING_KEY: "short", HEALTHCHECKS_PING_URL: PING }, "HEALTHCHECKS_PING_KEY"],
    [{ TARGETS: ONE, HEALTHCHECKS_PING_KEY: `${KEY.slice(1)}/`, HEALTHCHECKS_PING_URL: PING }, "HEALTHCHECKS_PING_KEY"],
    [{ TARGETS: ONE, HEALTHCHECKS_PING_KEY: KEY }, "HEALTHCHECKS_PING_URL"],
    [
      { TARGETS: ONE, HEALTHCHECKS_PING_KEY: KEY, HEALTHCHECKS_PING_URL: "https://evil.example/12345678-1234-1234-1234-123456789abc" },
      "HEALTHCHECKS_PING_URL",
    ],
  ])("refuses a missing or odd secret %#", (env, name) => {
    expect(() => readSettings(env)).toThrow(name);
  });
});

describe("targets", () => {
  const site = (extra: Record<string, unknown>) =>
    JSON.stringify({ eva: { name: "Eva", sites: [{ url: "https://a.example.com", expect: "ok", ...extra }] } });

  it.each([
    [undefined, "not valid JSON"],
    ["{", "not valid JSON"],
    ["[]", "must map each project"],
    ["{}", "must map each project"],
    [JSON.stringify({ Eva: { name: "Eva", sites: [] } }), "lowercase"],
    [JSON.stringify({ eva: { name: "Eva", sites: [], extra: 1 } }), "only a name and sites"],
    [JSON.stringify({ eva: ["x"] }), "only a name and sites"],
    [JSON.stringify({ eva: { name: "Eva<script>", sites: [] } }), "name is not usable"],
    [JSON.stringify({ eva: { name: "Eva", sites: [] } }), "list of sites"],
    [JSON.stringify({ eva: { name: "Eva", sites: "x" } }), "list of sites"],
    [JSON.stringify({ eva: { name: "Eva", sites: ["x"] } }), "only a url, an expect, a part and a status"],
    [site({ note: "x" }), "only a url, an expect, a part and a status"],
    [site({ expect: "exact" }), 'expect must be "ok", "answer" or "status"'],
    [site({ expect: "status" }), 'give a status with expect "status"'],
    [site({ status: 405 }), 'give a status with expect "status"'],
    [site({ expect: "answer", status: 405 }), 'give a status with expect "status"'],
    [site({ expect: "status", status: 500 }), "whole number from 100 to 499"],
    [site({ expect: "status", status: 99 }), "whole number from 100 to 499"],
    [site({ expect: "status", status: 405.5 }), "whole number from 100 to 499"],
    [site({ expect: "status", status: "405" }), "whole number from 100 to 499"],
    [site({ part: "API" }), "a part is not lowercase"],
    [site({ part: "sign in" }), "a part is not lowercase"],
    [site({ part: "a".repeat(25) }), "a part is not lowercase"],
    [site({ part: 7 }), "a part is not lowercase"],
    [site({ url: "http://a.example.com" }), "https address"],
    [site({ url: "https://a.example.com/?q=1" }), "https address"],
    [site({ url: "https://user@a.example.com" }), "https address"],
    [site({ url: `https://a.example.com/${"x".repeat(300)}` }), "https address"],
    [site({ url: 42 }), "https address"],
    [site({ url: "https://10.0.0.1/health" }), "not an IP address"],
    [site({ expect: "maybe" }), 'expect must be "ok", "answer" or "status"'],
  ])("refuses what it cannot use exactly %#", (raw, message) => {
    expect(() => readTargets(raw)).toThrow(SettingsError);
    expect(() => readTargets(raw)).toThrow(message);
  });

  it("refuses an address listed twice", () => {
    const twice = [
      { url: "https://a.example.com", expect: "ok" },
      { url: "https://a.example.com", expect: "answer" },
    ];
    expect(() => readTargets(JSON.stringify({ eva: { name: "Eva", sites: twice } }))).toThrow("listed twice");
  });

  it("holds only what one run can check within the free plan's 50 outbound calls", () => {
    expect(callsNeeded(10, 3)).toBe(47);
    const sites = (n: number) => Array.from({ length: n }, (_, i) => ({ url: `https://s${i}.example.com`, expect: "ok" }));
    // One project: 4 x 11 + 2 + 1 = 47 fits; 4 x 12 + 2 + 1 = 51 does not.
    expect(readTargets(JSON.stringify({ eva: { name: "Eva", sites: sites(11) } }))).toHaveLength(11);
    expect(() => readTargets(JSON.stringify({ eva: { name: "Eva", sites: sites(12) } }))).toThrow(`at most ${MAX_CALLS} calls`);
  });

  it("counts Worker guards in the 5-minute run's budget, not the address checks'", () => {
    const sites = (n: number) => Array.from({ length: n }, (_, i) => ({ url: `https://s${i}.example.com`, expect: "ok" }));
    const guard = { worker: "api-worker", part: "api", secrets: ["A_KEY"], not_from: [] };
    const base = { HEALTHCHECKS_PING_KEY: KEY, HEALTHCHECKS_PING_URL: PING, CLOUDFLARE_ACCOUNT_ID: "0".repeat(32), CLOUDFLARE_READ_TOKEN: "t".repeat(40) };
    const guards = (n: number) => JSON.stringify({ "eva-workers": { name: "Eva", guards: Array(n).fill(guard) } });
    // The addresses may use all 47 of their run; the guards run elsewhere. There, the reminders' 11
    // plus 4 per guard (two reads, each maybe twice) and 1 per project must fit 50.
    const targets = JSON.stringify({ eva: { name: "Eva", sites: sites(11) } });
    expect(readSettings({ ...base, TARGETS: targets, WORKER_GUARDS: guards(5) }).guards?.projects).toHaveLength(1); // 11 + 21
    const two = JSON.stringify({ a: { name: "A", guards: Array(5).fill(guard) }, b: { name: "B", guards: Array(5).fill(guard) } });
    expect(() => readSettings({ ...base, TARGETS: targets, WORKER_GUARDS: two })).toThrow("the reminders and WORKER_GUARDS together need more than the 50 calls"); // 11 + 42
    expect(readSettings({ ...base, TARGETS: targets }).guards).toBeNull();
  });
});
