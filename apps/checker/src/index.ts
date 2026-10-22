// Yaqzan's external uptime checker (ADR-032): a Cloudflare Worker run every minute by cron.
//
// Each run checks every address twice (at 0 s and 30 s). A failure is re-checked 10 s later
// and counts only if that fails too, so a site that goes down is known within about a minute.
// Changes are reported to each project's Healthchecks.io check, which alerts the Chairman's
// phone (ntfy) and email: Healthchecks sends from its own servers, because ntfy.sh refuses
// Cloudflare's shared addresses (http 429). Hourly counts feed the availability SLO. Each run
// ends with a heartbeat to Healthchecks, which alerts if the checker itself stops. The run at minute 0
// also checks each guarded Worker's settings and deploys (guard.ts) and reports to its own check.
//
// The Worker has no web address of its own and never reads a page: status codes only.

import { changes, reports, type Report, type Seen } from "./alerts";
import { PING_URL_RE, readSettings, type RawEnv, type Settings } from "./config";
import { checkGuard, guardReport, type GuardSettings } from "./guard";
import { checkAll, type Deps } from "./probe";
import { readReminders, runReminders, type RawReminderEnv } from "./reminders";
import { addressId, loadState, saveRound } from "./store";

export interface Env extends RawEnv, RawReminderEnv {
  readonly DB: D1Database;
}

export const SECOND_ROUND_MS = 30_000;
const SEND_TIMEOUT_MS = 5_000;

export async function run(env: Env, deps: Deps): Promise<void> {
  const start = deps.now();
  let settings: Settings | null = null;
  try {
    settings = readSettings(env);
    // The first run of each hour also reports every project (keeps its check fresh) and prunes old counts.
    const hourly = new Date(start).getUTCMinutes() === 0;
    await round(env.DB, settings, deps, hourly);
    await deps.wait(Math.max(0, start + SECOND_ROUND_MS - deps.now()));
    await round(env.DB, settings, deps, false);
    if (hourly && settings.guards !== null) await guardRound(settings.guards, settings.pingKey, deps);
    await send(deps, settings.pingUrl, { method: "POST" });
  } catch (error) {
    // Tell Healthchecks at once that the run failed (it alerts); then let Cloudflare log it.
    const ping = settings?.pingUrl ?? (env.HEALTHCHECKS_PING_URL ?? "").trim();
    if (PING_URL_RE.test(ping)) await send(deps, `${ping}/fail`, { method: "POST" });
    throw error;
  }
}

async function round(db: D1Database, settings: Settings, deps: Deps, hourly: boolean): Promise<void> {
  const checked = await checkAll(settings.targets, deps);
  const seen: Seen[] = await Promise.all(checked.map(async (c) => ({ ...c, id: await addressId(c.target.url) })));
  const stored = await loadState(db);
  const changed = changes(seen, stored);
  // Report before saving, and keep a project's old state if its report did not arrive:
  // the next round sees the same change and reports again (twice beats never).
  const unreported = new Set<string>();
  for (const report of reports(seen, changed, stored, hourly)) {
    if (!(await tell(deps, settings.pingKey, report))) unreported.add(report.project);
  }
  const saved = changed.filter((c) => !unreported.has(c.project));
  await saveRound(db, seen, saved, deps.now(), hourly);
  const down = seen.filter((s) => !s.up);
  const why = down.map((s) => `${s.target.project}/${s.id}=${s.reason}`).join(" ");
  console.log(`checked ${seen.length} addresses: ${seen.length - down.length} up, ${down.length} down, ${changed.length} changed ${why}`.trim());
}

// Hourly: every guarded Worker, one plain-English report per project to its own check. A guard
// never stops the uptime checks: anything unexpected is reported as "could not read".
async function guardRound(guards: GuardSettings, pingKey: string, deps: Deps): Promise<void> {
  for (const project of guards.projects) {
    const results = [];
    for (const guard of project.guards) results.push(await checkGuard(guard, guards, deps));
    const report = guardReport(project, results);
    const url = `https://hc-ping.com/${pingKey}/${report.slug}${report.down ? "/fail" : ""}`;
    const told = await send(deps, url, { method: "POST", body: report.message, headers: { "content-type": "text/plain" } });
    const state = report.down ? "a problem was found" : "all guards passed";
    console.log(`Worker guards for ${project.name}: ${state}; ${told ? "reported" : "report not delivered, retried next hour"}. ${report.message}`);
  }
}

// The project's own check: /fail when something is down, plain when all is up; our words as the body.
async function tell(deps: Deps, pingKey: string, report: Report): Promise<boolean> {
  const url = `https://hc-ping.com/${pingKey}/${report.project}${report.down ? "/fail" : ""}`;
  return send(deps, url, { method: "POST", body: report.message, headers: { "content-type": "text/plain" } });
}

// One outbound call that never throws: a failed report or heartbeat is logged, not fatal.
async function send(deps: Deps, url: string, init: RequestInit): Promise<boolean> {
  const started = deps.now();
  try {
    const response = await deps.fetch(url, { ...init, redirect: "manual", signal: AbortSignal.timeout(SEND_TIMEOUT_MS) });
    await response.body?.cancel();
    if (!response.ok) console.log(`send failed: http ${response.status}`);
    return response.ok;
  } catch (error) {
    // The error's kind and timing only: its message could hold the address (which holds a key).
    const kind = error instanceof Error ? error.name : "unknown";
    console.log(`send failed: no answer (${kind} after ${deps.now() - started} ms)`);
    return false;
  }
}

const live: Deps = {
  fetch: async (url, init) => fetch(url, init),
  wait: async (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now: () => Date.now(),
};

// The second schedule: reminders until acknowledged (reminders.ts), in its own run and call budget.
export const REMINDER_CRON = "*/5 * * * *";

export async function remind(env: Env, deps: Deps): Promise<void> {
  const settings = readReminders(env);
  if (settings === null) return;
  await runReminders(env.DB, settings, deps);
}

export default {
  async scheduled(controller, env) {
    if (controller.cron === REMINDER_CRON) await remind(env, live);
    else await run(env, live);
  },
} satisfies ExportedHandler<Env>;
