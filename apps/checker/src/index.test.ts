import { describe, expect, it } from "vitest";

import { FakeD1, fakeDeps } from "./fakes.test-support";
import worker, { run, SECOND_ROUND_MS, type Env } from "./index";
import { RECHECK_AFTER_MS } from "./probe";
import { addressId, KEEP_HOURS } from "./store";

const KEY = "abcdefghijklmnopqrstuv";
const PING = "https://hc-ping.com/12345678-1234-1234-1234-123456789abc";
const EVA = `https://hc-ping.com/${KEY}/eva`;
const A = "https://a.example.com";
const B = "https://b.example.com/health";

function env(db: FakeD1, targets?: string): Env {
  return {
    DB: db.asD1(),
    TARGETS:
      targets ??
      JSON.stringify({
        eva: {
          name: "Eva",
          sites: [
            { url: A, expect: "ok" },
            { url: B, expect: "answer" },
          ],
        },
      }),
    HEALTHCHECKS_PING_KEY: KEY,
    HEALTHCHECKS_PING_URL: PING,
  };
}

describe("one run", () => {
  it("all up: two rounds 30 s apart, counts saved, no report, then the heartbeat", async () => {
    const db = new FakeD1();
    const { deps, sent, waits } = fakeDeps({ [A]: [200], [B]: [404] }); // B only needs an answer
    await run(env(db), deps);
    expect(waits).toEqual([SECOND_ROUND_MS]);
    expect(sent.map((s) => s.url)).toEqual([PING]);
    const counts = [...db.hourly.values()];
    expect(counts.map((c) => [c.total, c.good])).toEqual([
      [2, 2],
      [2, 2],
    ]);
    expect([...db.state.values()].map((s) => s.up)).toEqual([1, 1]);
    expect(db.deleted).toEqual([]);
  });

  it("a site confirmed down: one fail report to the project's check, in fixed words, never the address", async () => {
    const db = new FakeD1();
    const first = fakeDeps({ [A]: [200], [B]: [503] });
    await run(env(db), first.deps);
    const reports = first.sent.filter((s) => s.url.startsWith(EVA));
    expect(reports.map((s) => s.url)).toEqual([`${EVA}/fail`]);
    expect(reports[0]?.init.body).toBe("1 of 2 addresses down (server error). Details on the dashboard.");
    expect(reports[0]?.init.body).not.toMatch(/example\.com/);
    expect(first.waits).toEqual([RECHECK_AFTER_MS, SECOND_ROUND_MS - RECHECK_AFTER_MS, RECHECK_AFTER_MS]);
    expect(db.state.get(await addressId(B))?.up).toBe(0);

    // Still down next minute: nothing new. Then back up: one success report.
    const still = fakeDeps({ [A]: [200], [B]: [503] });
    await run(env(db), still.deps);
    expect(still.sent.filter((s) => s.url.startsWith(EVA))).toEqual([]);
    const back = fakeDeps({ [A]: [200], [B]: [200] });
    await run(env(db), back.deps);
    expect(back.sent.filter((s) => s.url.startsWith(EVA)).map((s) => s.url)).toEqual([EVA]);
  });

  it("worst case: a site that dies just after a check is reported within 60 seconds", async () => {
    // Round one sees it up; it dies; round two (30 s) fails, the re-check (40 s) fails, report.
    const db = new FakeD1();
    const { deps, sent } = fakeDeps({ [A]: [200, 503, 503], [B]: [200] });
    const start = deps.now();
    let reportedAt = -1;
    const timed = {
      ...deps,
      fetch: async (url: string, init: RequestInit) => {
        if (url.startsWith(EVA)) reportedAt = deps.now();
        return deps.fetch(url, init);
      },
    };
    await run(env(db), timed);
    expect(sent.some((s) => s.url === `${EVA}/fail`)).toBe(true);
    // Each check may also take up to its 8 s timeout, which the fake clock does not spend: add 2 x 8 s.
    expect(reportedAt - start + 2 * 8_000).toBeLessThanOrEqual(60_000);
  });

  it("hourly (the run at minute 0): reports every project and forgets counts older than 90 days", async () => {
    const db = new FakeD1();
    const at = Date.UTC(2026, 9, 20, 23, 0, 1);
    const { deps, sent } = fakeDeps({ [A]: [200], [B]: [200] }, at);
    await run(env(db), deps);
    expect(db.deleted).toEqual([Math.floor(at / 3_600_000) - KEEP_HOURS]);
    expect(sent.map((s) => s.url)).toEqual([EVA, PING]);
    expect(sent[0]?.init.body).toBe("2 of 2 addresses up.");
  });

  it("bad settings: tells Healthchecks the run failed, then fails loudly", async () => {
    const db = new FakeD1();
    const { deps, sent } = fakeDeps({});
    await expect(run(env(db, "{"), deps)).rejects.toThrow("not valid JSON");
    expect(sent.map((s) => s.url)).toEqual([`${PING}/fail`]);
  });

  it("an unusable ping URL is never called, even to report a failure", async () => {
    const db = new FakeD1();
    const { deps, sent } = fakeDeps({});
    await expect(run({ ...env(db), HEALTHCHECKS_PING_URL: "https://evil.example/x" }, deps)).rejects.toThrow();
    expect(sent).toEqual([]);
  });

  it("a failed save still reported first, reports the failed run, and the next run reports again", async () => {
    const db = new FakeD1();
    db.failBatch = true;
    const { deps, sent } = fakeDeps({ [A]: [503], [B]: [200] });
    await expect(run(env(db), deps)).rejects.toThrow("D1 unavailable");
    expect(sent.map((s) => s.url)).toEqual([`${EVA}/fail`, `${PING}/fail`]);
  });

  it("a failed report or heartbeat never stops the run", async () => {
    const db = new FakeD1();
    const { deps } = fakeDeps({ [A]: [503], [B]: [200], [`${EVA}/fail`]: [null], [PING]: [500] });
    await expect(run(env(db), deps)).resolves.toBeUndefined();
    expect(db.hourly.size).toBe(2); // the checks still count
  });

  it("a report that did not arrive is sent again next round (its project's change is not saved)", async () => {
    const db = new FakeD1();
    // The first report times out; the second round's report arrives.
    const { deps, calls } = fakeDeps({ [A]: [503], [B]: [200], [`${EVA}/fail`]: [null, 200] });
    await run(env(db), deps);
    expect(calls.filter((u) => u === `${EVA}/fail`)).toHaveLength(2);
    expect(db.state.get(await addressId(A))?.up).toBe(0);
  });

  it("the Worker's cron entry point runs the checker", async () => {
    const db = new FakeD1();
    const bad = { ...env(db, "{"), HEALTHCHECKS_PING_URL: "" };
    await expect(worker.scheduled({} as ScheduledController, bad)).rejects.toThrow("HEALTHCHECKS_PING_URL");
  });
});
