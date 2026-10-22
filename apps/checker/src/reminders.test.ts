import { describe, expect, it } from "vitest";

import worker, { remind, REMINDER_CRON, type Env } from "./index";
import type { Deps } from "./probe";
import {
  decide,
  EVERY_MS,
  pingsFor,
  quiet,
  QUIET_EVERY_MS,
  readReminders,
  readStatuses,
  REFRESH_MS,
  runReminders,
  type CheckStatus,
  type Remembered,
  type Reminder,
  type ReminderSettings,
} from "./reminders";

const READ = "r".repeat(32);
const PING = "p".repeat(22);
const API = "https://healthchecks.io/api/v3/checks/";
const Z: Reminder = { project: "zaaheen", name: "Zaaheen", watch: ["zaaheen", "zaaheen-workers"], reminder: "zaaheen-reminder" };
const SETTINGS: ReminderSettings = { reminders: [Z], readKey: READ, pingKey: PING };
const NOON = Date.UTC(2026, 9, 22, 12, 0, 0); // 16:00 UAE: not quiet
const NIGHT = Date.UTC(2026, 9, 22, 21, 0, 0); // 01:00 UAE: quiet
const FRESH: Remembered = { downSince: null, lastSent: null, lastRefresh: null };
const MIN = 60_000;

const statuses = (s: Record<string, CheckStatus>) => new Map(Object.entries(s));

describe("readReminders", () => {
  const env = (r: unknown, extra: Record<string, string> = {}) => ({
    REMINDERS: typeof r === "string" ? r : JSON.stringify(r),
    HEALTHCHECKS_READ_KEY: READ,
    HEALTHCHECKS_PING_KEY: PING,
    ...extra,
  });
  const one = { zaaheen: { name: "Zaaheen", watch: ["zaaheen", "zaaheen-workers"], reminder: "zaaheen-reminder" } };

  it("is off when REMINDERS is not set", () => {
    expect(readReminders({})).toBeNull();
    expect(readReminders({ REMINDERS: " " })).toBeNull();
  });

  it("reads each project's reminder", () => {
    expect(readReminders(env(one))).toEqual(SETTINGS);
  });

  it.each([
    [env(one, { HEALTHCHECKS_READ_KEY: "short" }), "HEALTHCHECKS_READ_KEY"],
    [env(one, { HEALTHCHECKS_READ_KEY: `${"r".repeat(31)}!` }), "HEALTHCHECKS_READ_KEY"],
    [env(one, { HEALTHCHECKS_PING_KEY: "short" }), "HEALTHCHECKS_PING_KEY"],
    [env("{"), "not valid JSON"],
    [env([]), "map each project"],
    [env({}), "1 to 5 projects"],
    [env(Object.fromEntries(Array.from({ length: 6 }, (_, i) => [`p${i}`, { name: "P", watch: ["a"], reminder: `r${i}` }]))), "1 to 5 projects"],
    [env({ Zaaheen: one.zaaheen }), "lowercase words"],
    [env({ zaaheen: { ...one.zaaheen, extra: 1 } }), "only a name, watch and reminder"],
    [env({ zaaheen: "x" }), "only a name, watch and reminder"],
    [env({ zaaheen: { ...one.zaaheen, name: "Zaa<b>" } }), "name is not usable"],
    [env({ zaaheen: { ...one.zaaheen, watch: [] } }), "1 to 5 check slugs"],
    [env({ zaaheen: { ...one.zaaheen, watch: ["a", "b", "c", "d", "e", "f"] } }), "1 to 5 check slugs"],
    [env({ zaaheen: { ...one.zaaheen, watch: ["Bad Slug"] } }), "1 to 5 check slugs"],
    [env({ zaaheen: { ...one.zaaheen, watch: "zaaheen" } }), "1 to 5 check slugs"],
    [env({ zaaheen: { ...one.zaaheen, reminder: "zaaheen" } }), "its own check slug"],
    [env({ zaaheen: { ...one.zaaheen, reminder: "Bad" } }), "its own check slug"],
    [env({ zaaheen: one.zaaheen, eva: { name: "Eva", watch: ["eva"], reminder: "zaaheen-reminder" } }), "share a reminder"],
  ])("refuses an odd setting %#", (raw, message) => {
    expect(() => readReminders(raw)).toThrow(message);
  });

  it("allows five projects and five watched checks", () => {
    const five = Object.fromEntries(Array.from({ length: 5 }, (_, i) => [`p${i}`, { name: "P", watch: ["a", "b", "c", "d", "e"], reminder: `r${i}` }]));
    expect(readReminders(env(five))?.reminders).toHaveLength(5);
  });
});

function fakeHttp(answers: Record<string, { status: number; body?: unknown; raw?: string } | null>, now = NOON) {
  const calls: { url: string; init: RequestInit }[] = [];
  const deps: Deps = {
    fetch: (url, init) => {
      calls.push({ url, init });
      const a = answers[url];
      if (a === null) return Promise.reject(new TypeError("network error"));
      if (a === undefined) return Promise.resolve(new Response(null, { status: 200 })); // pings
      return Promise.resolve(new Response(a.raw ?? JSON.stringify(a.body ?? {}), { status: a.status }));
    },
    wait: () => Promise.resolve(),
    now: () => now,
  };
  return { deps, calls };
}

describe("readStatuses", () => {
  it("reads every check's status in one read-only call", async () => {
    const body = { checks: [{ slug: "zaaheen", status: "down" }, { slug: "zaaheen-reminder", status: "paused" }, { slug: "x", status: "grace" }, { slug: 7 }, "odd"] };
    const { deps, calls } = fakeHttp({ [API]: { status: 200, body } });
    expect(await readStatuses(SETTINGS, deps)).toEqual(statuses({ zaaheen: "down", "zaaheen-reminder": "paused", x: "other" }));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.init.method).toBe("GET");
    expect(new Headers(calls[0]?.init.headers).get("x-api-key")).toBe(READ);
  });

  it.each([
    ["refused", { status: 401 }],
    ["refused with a body that looks fine", { status: 403, body: { checks: [] } }],
    ["not JSON", { status: 200, raw: "<html>" }],
    ["too large, even when it reads as normal", { status: 200, body: { checks: [{ slug: "zaaheen", status: "down" }], pad: "x".repeat(256 * 1024) } }],
    ["no list", { status: 200, body: { checks: "x" } }],
    ["not an object", { status: 200, body: [] }],
    ["no answer", null],
  ] as const)("%s: unread", async (_name, answer) => {
    const { deps } = fakeHttp({ [API]: answer });
    expect(await readStatuses(SETTINGS, deps)).toBeNull();
  });
});

describe("decide", () => {
  const down = statuses({ zaaheen: "down", "zaaheen-workers": "up", "zaaheen-reminder": "up" });

  it("a new outage is remembered, and the first reminder waits a full interval (the check itself just alerted)", () => {
    const d = decide(Z, down, FRESH, NOON);
    expect(d.action).toBe("none");
    expect(d.next).toEqual({ downSince: NOON, lastSent: NOON, lastRefresh: null });
    expect(d.why).toBe("Zaaheen has been down for 0 min; the next reminder is due within 30 min.");
  });

  it("any watched check down counts, and nothing else does", () => {
    expect(decide(Z, statuses({ "zaaheen-workers": "down" }), FRESH, NOON).next.downSince).toBe(NOON);
    expect(decide(Z, statuses({ zaaheen: "paused", "zaaheen-workers": "other", eva: "down" }), FRESH, NOON).next.downSince).toBeNull();
  });

  it("30 minutes on, unacknowledged: remind, and say how long it has been down", () => {
    const before = { downSince: NOON - 95 * MIN, lastSent: NOON - EVERY_MS, lastRefresh: null };
    const d = decide(Z, down, before, NOON);
    expect(d.action).toBe("remind");
    expect(d.next).toEqual({ ...before, lastSent: NOON });
    expect(d.why).toBe("Zaaheen has been down for 1 h 35 min and nobody has acknowledged it, so the phone is reminded (every 30 min).");
  });

  it("not yet due: waits, and says when", () => {
    const d = decide(Z, down, { downSince: NOON - 40 * MIN, lastSent: NOON - EVERY_MS + MIN, lastRefresh: null }, NOON);
    expect(d.action).toBe("none");
    expect(d.why).toBe("Zaaheen has been down for 40 min; the next reminder is due within 1 min.");
  });

  it("quiet hours: hourly, and says so", () => {
    const before = { downSince: NIGHT - 3 * 60 * MIN, lastSent: NIGHT - EVERY_MS, lastRefresh: null };
    expect(decide(Z, down, before, NIGHT).action).toBe("none");
    const later = decide(Z, down, { ...before, lastSent: NIGHT - QUIET_EVERY_MS }, NIGHT);
    expect(later.action).toBe("remind");
    expect(later.why).toBe("Zaaheen has been down for 3 h and nobody has acknowledged it, so the phone is reminded (every 1 h in quiet hours).");
  });

  it("acknowledged (reminder paused): no reminders, the outage is still remembered", () => {
    const before = { downSince: NOON - 2 * 60 * MIN, lastSent: NOON - 90 * MIN, lastRefresh: null };
    const d = decide(Z, statuses({ zaaheen: "down", "zaaheen-reminder": "paused" }), before, NOON);
    expect(d.action).toBe("none");
    expect(d.next).toEqual(before);
    expect(d.why).toBe("Zaaheen has been down for 2 h; the Chairman acknowledged it, so no reminder is sent.");
  });

  it("healthy again: a paused or ringing reminder check is reset for the next outage", () => {
    for (const own of ["paused", "down"] as const) {
      const d = decide(Z, statuses({ zaaheen: "up", "zaaheen-reminder": own }), { downSince: 1, lastSent: 2, lastRefresh: 3 }, NOON);
      expect(d.action).toBe("reset");
      expect(d.next).toEqual({ downSince: null, lastSent: null, lastRefresh: NOON });
      expect(d.why).toBe("Zaaheen is healthy again, so its reminder check is reset for the next outage.");
    }
  });

  it("healthy: the reminder check is kept fresh every 6 hours, and otherwise left alone", () => {
    const ok = statuses({ zaaheen: "up", "zaaheen-reminder": "up" });
    expect(decide(Z, ok, FRESH, NOON).action).toBe("refresh");
    expect(decide(Z, ok, { ...FRESH, lastRefresh: NOON - REFRESH_MS }, NOON).action).toBe("refresh");
    const quietDay = decide(Z, ok, { ...FRESH, lastRefresh: NOON - REFRESH_MS + MIN }, NOON);
    expect(quietDay).toEqual({ action: "none", next: { downSince: null, lastSent: null, lastRefresh: NOON - REFRESH_MS + MIN }, why: "Zaaheen is healthy." });
    expect(decide(Z, ok, FRESH, NOON).why).toBe("Zaaheen is healthy; its reminder check is kept fresh so it never alerts by itself.");
  });
});

describe("quiet hours (23:00-07:00 UAE)", () => {
  it.each([
    [18, 59, false],
    [19, 0, true],
    [23, 59, true],
    [0, 0, true],
    [2, 59, true],
    [3, 0, false],
  ])("%s:%s UTC quiet=%s", (h, m, expected) => {
    expect(quiet(Date.UTC(2026, 9, 22, h, m))).toBe(expected);
  });
});

describe("pingsFor", () => {
  it("remind goes up then down; reset and refresh only up; none nothing", () => {
    const base = `https://hc-ping.com/${PING}/zaaheen-reminder`;
    expect(pingsFor("remind", PING, "zaaheen-reminder")).toEqual([base, `${base}/fail`]);
    expect(pingsFor("reset", PING, "zaaheen-reminder")).toEqual([base]);
    expect(pingsFor("refresh", PING, "zaaheen-reminder")).toEqual([base]);
    expect(pingsFor("none", PING, "zaaheen-reminder")).toEqual([]);
  });
});

// Just enough of D1 for the reminders table.
class RemindersD1 {
  readonly rows = new Map<string, Remembered>();
  // How many of the next saves fail once (a brief database blip).
  blips = 0;
  prepare = (sql: string) => {
    let args: unknown[] = [];
    const statement = {
      bind: (...values: unknown[]) => {
        args = values;
        return statement;
      },
      all: () =>
        Promise.resolve({
          results: [...this.rows.entries()].map(([project, r]) => ({ project, down_since: r.downSince, last_sent: r.lastSent, last_refresh: r.lastRefresh })),
        }),
      run: () => {
        if (this.blips > 0) {
          this.blips -= 1;
          return Promise.reject(new Error("D1 blip"));
        }
        if (!sql.startsWith("INSERT INTO reminders")) throw new Error(`unexpected: ${sql}`);
        const [project, downSince, lastSent, lastRefresh] = args as [string, number | null, number | null, number | null];
        this.rows.set(project, { downSince, lastSent, lastRefresh });
        return Promise.resolve();
      },
    };
    return statement;
  };
  asD1(): D1Database {
    return this as unknown as D1Database;
  }
}

describe("runReminders", () => {
  const checks = (s: Record<string, string>) => ({ [API]: { status: 200, body: { checks: Object.entries(s).map(([slug, status]) => ({ slug, status })) } } });
  const REMIND = `https://hc-ping.com/${PING}/zaaheen-reminder`;

  it("a due reminder pings up then down and is remembered as sent", async () => {
    const db = new RemindersD1();
    db.rows.set("zaaheen", { downSince: NOON - 60 * MIN, lastSent: NOON - EVERY_MS, lastRefresh: null });
    const { deps, calls } = fakeHttp(checks({ zaaheen: "down", "zaaheen-reminder": "down" }));
    await runReminders(db.asD1(), SETTINGS, deps);
    expect(calls.map((c) => c.url)).toEqual([API, REMIND, `${REMIND}/fail`]);
    expect(calls.slice(1).every((c) => c.init.method === "POST")).toBe(true);
    expect(db.rows.get("zaaheen")?.lastSent).toBe(NOON);
  });

  it("a reminder Healthchecks did not take is not remembered as sent, so the next run tries again", async () => {
    const db = new RemindersD1();
    db.rows.set("zaaheen", { downSince: NOON - 60 * MIN, lastSent: NOON - EVERY_MS, lastRefresh: null });
    const { deps } = fakeHttp({ ...checks({ zaaheen: "down" }), [`${REMIND}/fail`]: null });
    await runReminders(db.asD1(), SETTINGS, deps);
    expect(db.rows.get("zaaheen")).toEqual({ downSince: NOON - 60 * MIN, lastSent: NOON - EVERY_MS, lastRefresh: null });
  });

  it("a database blip on saving is tried again, so the reminder is still remembered", async () => {
    const db = new RemindersD1();
    db.rows.set("zaaheen", { downSince: NOON - 60 * MIN, lastSent: NOON - EVERY_MS, lastRefresh: null });
    db.blips = 1;
    const { deps } = fakeHttp(checks({ zaaheen: "down", "zaaheen-reminder": "down" }));
    await runReminders(db.asD1(), SETTINGS, deps);
    expect(db.rows.get("zaaheen")?.lastSent).toBe(NOON);
  });

  it("a refused ping is also not remembered", async () => {
    const db = new RemindersD1();
    const { deps } = fakeHttp({ ...checks({ zaaheen: "up", "zaaheen-reminder": "up" }), [REMIND]: { status: 500 } });
    await runReminders(db.asD1(), SETTINGS, deps);
    expect(db.rows.get("zaaheen")?.lastRefresh).toBeNull();
  });

  it("Healthchecks unreadable: no pings and nothing changes", async () => {
    const db = new RemindersD1();
    db.rows.set("zaaheen", { downSince: 1, lastSent: 2, lastRefresh: 3 });
    const { deps, calls } = fakeHttp({ [API]: { status: 500 } });
    await runReminders(db.asD1(), SETTINGS, deps);
    expect(calls).toHaveLength(1);
    expect(db.rows.get("zaaheen")).toEqual({ downSince: 1, lastSent: 2, lastRefresh: 3 });
  });

  it("a first run with all healthy keeps the reminder check fresh", async () => {
    const db = new RemindersD1();
    const { deps, calls } = fakeHttp(checks({ zaaheen: "up", "zaaheen-reminder": "up" }));
    await runReminders(db.asD1(), SETTINGS, deps);
    expect(calls.map((c) => c.url)).toEqual([API, REMIND]);
    expect(db.rows.get("zaaheen")).toEqual({ downSince: null, lastSent: null, lastRefresh: NOON });
  });
});

describe("the 5-minute schedule", () => {
  const env = (extra: Partial<Env>, db = new RemindersD1()): Env => ({ DB: db.asD1(), ...extra });

  it("runs the reminders, and nothing when they are not set up", async () => {
    const { deps, calls } = fakeHttp({ [API]: { status: 200, body: { checks: [] } } });
    await remind(env({}), deps);
    expect(calls).toHaveLength(0);
    await remind(env({ REMINDERS: JSON.stringify({ zaaheen: { name: "Zaaheen", watch: ["zaaheen"], reminder: "zaaheen-reminder" } }), HEALTHCHECKS_READ_KEY: READ, HEALTHCHECKS_PING_KEY: PING }), deps);
    expect(calls[0]?.url).toBe(API);
  });

  it("never runs at minute 0, where the hourly Worker guards run", () => {
    expect(REMINDER_CRON.split(" ")[0]).toBe("2-59/5");
  });

  it("the Worker sends the 5-minute cron to the reminders, never to the address checks", async () => {
    const bad = { DB: new RemindersD1().asD1(), REMINDERS: "{", HEALTHCHECKS_READ_KEY: READ, HEALTHCHECKS_PING_KEY: PING } as Env;
    await expect(worker.scheduled({ cron: REMINDER_CRON } as ScheduledController, bad)).rejects.toThrow("REMINDERS is not valid JSON");
    // The every-minute run reads TARGETS and the ping URL instead, so it fails on those.
    await expect(worker.scheduled({ cron: "* * * * *" } as ScheduledController, bad)).rejects.toThrow("HEALTHCHECKS_PING_URL");
  });
});
