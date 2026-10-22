import { describe, expect, it } from "vitest";

import { checkGuard, GUARD_RETRY_AFTER_MS, guardCalls, guardReport, readGuards, type Guard, type GuardProject, type GuardSettings } from "./guard";
import type { Deps } from "./probe";

const ACCOUNT = "0123456789abcdef0123456789abcdef";
const TOKEN = "t".repeat(40);
const BASE = `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT}/workers/scripts/api-worker`;
const GUARD: Guard = { worker: "api-worker", part: "api", secrets: ["A_KEY", "B_KEY"], notFrom: ["account.example.com from"] };
const PROJECT: GuardProject = { slug: "zaaheen-workers", name: "Zaaheen", guards: [GUARD] };
const SETTINGS: GuardSettings = { projects: [PROJECT], accountId: ACCOUNT, token: TOKEN };

const settingsBody = (...names: string[]) => ({
  success: true,
  result: { bindings: [...names.map((name) => ({ type: "secret_text", name })), { type: "kv_namespace", name: "C_KEY" }] },
});
const deploysBody = (...deploys: { created_on?: string; message?: string }[]) => ({
  success: true,
  result: {
    deployments: deploys.map((d) => ({
      ...(d.created_on === undefined ? {} : { created_on: d.created_on }),
      annotations: d.message === undefined ? {} : { "workers/message": d.message },
    })),
  },
});
const OWN = deploysBody({ created_on: "2026-10-22T12:05:49Z" });

type Answer = { status: number; body?: unknown; raw?: string } | null;

function cloudflare(answers: Record<string, Answer>) {
  const calls: { url: string; init: RequestInit }[] = [];
  const deps: Deps = {
    fetch: (url, init) => {
      calls.push({ url, init });
      const a = answers[url];
      if (a === undefined) throw new Error(`unexpected call: ${url}`);
      if (a === null) return Promise.reject(new TypeError("network error"));
      return Promise.resolve(new Response(a.raw ?? JSON.stringify(a.body ?? {}), { status: a.status }));
    },
    wait: () => Promise.resolve(),
    now: () => 0,
  };
  return { deps, calls };
}

describe("readGuards", () => {
  const env = (guards: unknown, extra: Record<string, string> = {}) => ({
    WORKER_GUARDS: typeof guards === "string" ? guards : JSON.stringify(guards),
    CLOUDFLARE_ACCOUNT_ID: ACCOUNT,
    CLOUDFLARE_READ_TOKEN: TOKEN,
    ...extra,
  });
  const one = (guard: Record<string, unknown>) => ({ "zaaheen-workers": { name: "Zaaheen", guards: [guard] } });
  const valid = { worker: "api-worker", part: "api", secrets: ["B_KEY", "A_KEY", "A_KEY"], not_from: ["account.example.com from"] };

  it("is off when WORKER_GUARDS is not set", () => {
    expect(readGuards({})).toBeNull();
    expect(readGuards({ WORKER_GUARDS: "  " })).toBeNull();
  });

  it("reads guards, with each secret list sorted and without repeats", () => {
    expect(readGuards(env(one(valid)))).toEqual({
      projects: [{ slug: "zaaheen-workers", name: "Zaaheen", guards: [{ ...GUARD, secrets: ["A_KEY", "B_KEY"] }] }],
      accountId: ACCOUNT,
      token: TOKEN,
    });
    expect(readGuards(env(one({ ...valid, not_from: [] })))?.projects[0]?.guards[0]?.notFrom).toEqual([]);
  });

  it.each([
    [env(one(valid), { CLOUDFLARE_ACCOUNT_ID: "nope" }), "CLOUDFLARE_ACCOUNT_ID"],
    [env(one(valid), { CLOUDFLARE_ACCOUNT_ID: "" }), "CLOUDFLARE_ACCOUNT_ID"],
    [env(one(valid), { CLOUDFLARE_READ_TOKEN: "short" }), "CLOUDFLARE_READ_TOKEN"],
    [env(one(valid), { CLOUDFLARE_READ_TOKEN: `${"t".repeat(39)}!` }), "CLOUDFLARE_READ_TOKEN"],
    [env("{"), "not valid JSON"],
    [env({}), "must map each check"],
    [env([]), "must map each check"],
    [env({ Zaaheen: { name: "Zaaheen", guards: [valid] } }), "slug is not lowercase"],
    [env({ z: { name: "Zaaheen", guards: [valid], extra: 1 } }), "only a name and guards"],
    [env({ z: { name: "Zaa<b>", guards: [valid] } }), "name is not usable"],
    [env({ z: { name: "Zaaheen", guards: [] } }), "1 to 5 guards"],
    [env({ z: { name: "Zaaheen", guards: Array(6).fill(valid) } }), "1 to 5 guards"],
    [env(one({ ...valid, note: "x" })), "only a worker, a part, secrets and not_from"],
    [env(one({ ...valid, worker: "Api_Worker" })), "worker name is not usable"],
    [env(one({ ...valid, worker: "-api" })), "worker name is not usable"],
    [env(one({ ...valid, part: "API" })), "part is not lowercase"],
    [env(one({ ...valid, part: "a".repeat(25) })), "part is not lowercase"],
    [env(one({ ...valid, secrets: [] })), "1 to 50 names in capitals"],
    [env(one({ ...valid, secrets: ["lower"] })), "1 to 50 names in capitals"],
    [env(one({ ...valid, secrets: Array.from({ length: 51 }, (_, i) => `K${i}`) })), "1 to 50 names in capitals"],
    [env(one({ ...valid, not_from: ["<script>"] })), "up to 5 short message starts"],
    [env(one({ ...valid, not_from: Array(6).fill("x from") })), "up to 5 short message starts"],
    [env(one({ ...valid, not_from: "x from" })), "up to 5 short message starts"],
  ])("refuses an odd setting %#", (raw, message) => {
    expect(() => readGuards(raw)).toThrow(message);
  });

  it("allows the longest names and lists", () => {
    const guard = { ...valid, worker: "a".repeat(63), part: "a".repeat(24), secrets: Array.from({ length: 50 }, (_, i) => `K${i}`) };
    expect(readGuards(env(one({ ...guard, not_from: Array(5).fill("x from") })))?.projects[0]?.guards[0]?.secrets).toHaveLength(50);
    expect(readGuards(env({ z: { name: "Z", guards: Array(5).fill(valid) } }))?.projects[0]?.guards).toHaveLength(5);
  });

  it("counts two reads per guard, each maybe twice, and one report per project", () => {
    expect(guardCalls([PROJECT, { ...PROJECT, guards: [GUARD, GUARD] }])).toBe(5 + 9);
    expect(guardCalls([])).toBe(0);
  });
});

describe("checkGuard", () => {
  it("all settings and its own deploy: ok, with a read-only GET and the token", async () => {
    const { deps, calls } = cloudflare({
      [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY", "EXTRA") },
      [`${BASE}/deployments`]: { status: 200, body: deploysBody({ created_on: "2026-10-22T12:05:49Z", message: "api deploy" }) },
    });
    expect((await checkGuard(GUARD, SETTINGS, deps)).verdict).toEqual({ kind: "ok" });
    expect(calls.map((c) => c.url)).toEqual([`${BASE}/settings`, `${BASE}/deployments`]);
    for (const c of calls) {
      expect(c.init.method).toBe("GET");
      expect(new Headers(c.init.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    }
  });

  it("missing settings: named from our own list, and the deploy history is not needed", async () => {
    const { deps, calls } = cloudflare({ [`${BASE}/settings`]: { status: 200, body: settingsBody("B_KEY") } });
    expect((await checkGuard({ ...GUARD, secrets: ["A_KEY", "B_KEY", "C_KEY"] }, SETTINGS, deps)).verdict).toEqual({
      kind: "missing",
      missing: ["A_KEY", "C_KEY"], // C_KEY exists, but as a different kind of binding: not a secret
    });
    expect(calls).toHaveLength(1);
  });

  it("the newest deploy by date decides, whatever order the list comes in", async () => {
    const body = deploysBody(
      { created_on: "2026-10-21T11:00:00Z", message: "api deploy" },
      { created_on: "2026-10-22T09:00:00Z", message: "account.example.com from 410837f" },
      { created_on: "2026-10-20T08:00:00Z" },
    );
    const { deps } = cloudflare({ [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body } });
    expect((await checkGuard(GUARD, SETTINGS, deps)).verdict).toEqual({ kind: "wrong_deploy" });
  });

  it("a deploy without a message, or one that only mentions the pattern later, is its own", async () => {
    for (const message of [undefined, "deploy of account.example.com from main"]) {
      const body = deploysBody({ created_on: "2026-10-22T12:05:49Z", ...(message === undefined ? {} : { message }) });
      const { deps } = cloudflare({ [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body } });
      expect((await checkGuard(GUARD, SETTINGS, deps)).verdict).toEqual({ kind: "ok" });
    }
  });

  it.each([
    ["settings refused", { [`${BASE}/settings`]: { status: 403 } }, 403],
    ["settings refused with a body that looks fine", { [`${BASE}/settings`]: { status: 403, body: settingsBody("A_KEY", "B_KEY") } }, 403],
    [
      "deploys refused with a body that looks fine",
      { [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 404, body: OWN } },
      404,
    ],
    ["settings no answer", { [`${BASE}/settings`]: null }, null],
    ["settings not JSON", { [`${BASE}/settings`]: { status: 200, raw: "<html>" } }, null],
    ["settings too large", { [`${BASE}/settings`]: { status: 200, raw: " ".repeat(256 * 1024 + 1) } }, 200],
    ["settings not a success", { [`${BASE}/settings`]: { status: 200, body: { success: false, result: { bindings: [] } } } }, 200],
    ["settings without bindings", { [`${BASE}/settings`]: { status: 200, body: { success: true, result: {} } } }, 200],
    ["settings without a result", { [`${BASE}/settings`]: { status: 200, body: { success: true } } }, 200],
    ["deploys refused", { [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 500 } }, 500],
    [
      "no deploys",
      { [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body: deploysBody() } },
      200,
    ],
    [
      "a deploy without a date",
      { [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body: deploysBody({ message: "x" }) } },
      200,
    ],
    [
      "deploys not a success",
      { [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body: { success: false } } },
      200,
    ],
  ] as [string, Record<string, Answer>, number | null][])("%s: unread, never ok", async (_name, answers, status) => {
    const { deps } = cloudflare(answers);
    expect((await checkGuard(GUARD, SETTINGS, deps)).verdict).toEqual({ kind: "unread", status });
  });

  it("a read that got no answer or a server error is tried once more, and a good second answer counts", async () => {
    for (const first of [null, { status: 500 }, { status: 429 }] as Answer[]) {
      const queue: Answer[] = [first, { status: 200, body: settingsBody("A_KEY", "B_KEY") }];
      const waits: number[] = [];
      const urls: string[] = [];
      const deps: Deps = {
        fetch: (url) => {
          urls.push(url);
          const a = url.endsWith("/settings") ? queue.shift() : { status: 200, body: OWN };
          if (a === null || a === undefined) return Promise.reject(new TypeError("network error"));
          return Promise.resolve(new Response(JSON.stringify(a.body ?? {}), { status: a.status }));
        },
        wait: (ms) => {
          waits.push(ms);
          return Promise.resolve();
        },
        now: () => 0,
      };
      expect((await checkGuard(GUARD, SETTINGS, deps)).verdict).toEqual({ kind: "ok" });
      expect(urls).toEqual([`${BASE}/settings`, `${BASE}/settings`, `${BASE}/deployments`]);
      expect(waits).toEqual([GUARD_RETRY_AFTER_MS]);
    }
  });

  it("a refusal is not retried: waiting cannot fix a token without access", async () => {
    for (const status of [401, 403, 404]) {
      const { deps, calls } = cloudflare({ [`${BASE}/settings`]: { status } });
      expect((await checkGuard(GUARD, SETTINGS, deps)).verdict).toEqual({ kind: "unread", status });
      expect(calls).toHaveLength(1);
    }
  });

  it("a deploy list with an entry that is not an object is unread", async () => {
    const body = { success: true, result: { deployments: ["x"] } };
    const { deps } = cloudflare({ [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body } });
    expect((await checkGuard(GUARD, SETTINGS, deps)).verdict.kind).toBe("unread");
  });

  it("uses the guard's own pattern list", async () => {
    const { deps } = cloudflare({ [`${BASE}/settings`]: { status: 200, body: settingsBody("A_KEY", "B_KEY") }, [`${BASE}/deployments`]: { status: 200, body: OWN } });
    expect((await checkGuard({ ...GUARD, notFrom: [] }, SETTINGS, deps)).verdict).toEqual({ kind: "ok" });
  });
});

describe("guardReport", () => {
  it("all fine: one plain line, not down", () => {
    expect(guardReport(PROJECT, [{ guard: GUARD, verdict: { kind: "ok" } }])).toEqual({
      slug: "zaaheen-workers",
      down: false,
      message: "Zaaheen api: all 2 settings present and its latest deploy is its own.",
    });
  });

  it("says what, why and what to check for each problem, one line per guard", () => {
    const report = guardReport(PROJECT, [
      { guard: GUARD, verdict: { kind: "missing", missing: ["A_KEY"] } },
      { guard: { ...GUARD, part: "site" }, verdict: { kind: "wrong_deploy" } },
      { guard: { ...GUARD, part: "web" }, verdict: { kind: "unread", status: 403 } },
      { guard: { ...GUARD, part: "app" }, verdict: { kind: "unread", status: null } },
      { guard: { ...GUARD, part: "ok" }, verdict: { kind: "ok" } },
    ]);
    expect(report.down).toBe(true);
    expect(report.message.split("\n")).toEqual([
      "Zaaheen api is missing 1 of its 2 settings (A_KEY), so whatever uses them will fail. Most likely a deploy replaced the Worker or a setting was deleted: check its deploy history, then re-enter the missing settings.",
      "Zaaheen site: its latest deploy is another service's publish, not its own, so it has probably been replaced. Check which Worker owns its address and redeploy it from its own config.",
      "Zaaheen web: could not read its settings from Cloudflare (http 403), so its health is unknown, not fine. The read-only token may have expired or lost access.",
      "Zaaheen app: could not read its settings from Cloudflare (no answer), so its health is unknown, not fine. The read-only token may have expired or lost access.",
      "Zaaheen ok: all 2 settings present and its latest deploy is its own.",
    ]);
  });

  it("an unread guard alone counts as down", () => {
    expect(guardReport(PROJECT, [{ guard: GUARD, verdict: { kind: "unread", status: null } }]).down).toBe(true);
  });
});
