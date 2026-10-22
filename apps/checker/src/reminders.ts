// Reminders until acknowledged (incident 2026-10-22): a phone alert goes out once, so a missed one
// was never repeated, and Zaaheen's API stayed down for about a day unnoticed.
//
// Every 5 minutes (its own cron run, with its own call budget) this reads every Healthchecks check
// in one read-only call. While a product's checks are down, it re-triggers that product's
// reminder check every 30 minutes (hourly in quiet hours, 23:00-07:00 UAE), which rings the
// Chairman's phone through ntfy again ("Zaaheen still down - reminder is DOWN").
//
// Acknowledging: the Chairman presses Pause on the reminder check in Healthchecks (the alert's
// "View" button opens it). A paused reminder is left alone until the product is healthy again;
// then a plain ping un-pauses it, ready for the next outage. Its "back up" alerts are muted in
// Healthchecks, so these pings never ring. No web address of our own is needed.
//
// What Healthchecks answers is only matched against our own slugs; every log line is our own words.

import { SettingsError } from "./errors";
import type { Deps } from "./probe";
import { withRetry } from "./store";

export interface Reminder {
  // Our project id, as in TARGETS.
  readonly project: string;
  readonly name: string;
  // The Healthchecks checks that count as this product being down.
  readonly watch: readonly string[];
  // The reminder check's slug.
  readonly reminder: string;
}

export interface ReminderSettings {
  readonly reminders: readonly Reminder[];
  readonly readKey: string;
  readonly pingKey: string;
}

export const EVERY_MS = 30 * 60_000;
export const QUIET_EVERY_MS = 60 * 60_000;
// The reminder check is pinged while all is well this often, so it never goes quiet by itself.
export const REFRESH_MS = 6 * 60 * 60_000;
// 23:00-07:00 UAE (UTC+4) is 19:00-03:00 UTC.
const QUIET_FROM_UTC = 19;
const QUIET_TO_UTC = 3;

const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const NAME_RE = /^[A-Za-z0-9 ]{1,40}$/;
const READ_KEY_RE = /^[A-Za-z0-9_-]{32}$/;
const PING_KEY_RE = /^[A-Za-z0-9_-]{22}$/;
const MAX_REMINDERS = 5;
const MAX_WATCH = 5;
const API = "https://healthchecks.io/api/v3/checks/";
const TIMEOUT_MS = 8_000;
const MAX_ANSWER_BYTES = 256 * 1024;

export interface RawReminderEnv {
  readonly REMINDERS?: string;
  readonly HEALTHCHECKS_READ_KEY?: string;
  readonly HEALTHCHECKS_PING_KEY?: string;
}

// REMINDERS: {"zaaheen": {"name": "Zaaheen", "watch": ["zaaheen", "zaaheen-workers"], "reminder": "zaaheen-reminder"}}
// Not set means no reminders. When it is set, the read key and ping key must be usable.
export function readReminders(env: RawReminderEnv): ReminderSettings | null {
  const raw = (env.REMINDERS ?? "").trim();
  if (raw === "") return null;
  const readKey = (env.HEALTHCHECKS_READ_KEY ?? "").trim();
  if (!READ_KEY_RE.test(readKey)) throw new SettingsError("HEALTHCHECKS_READ_KEY is missing or not a read-only API key");
  const pingKey = (env.HEALTHCHECKS_PING_KEY ?? "").trim();
  if (!PING_KEY_RE.test(pingKey)) throw new SettingsError("HEALTHCHECKS_PING_KEY is missing or not a ping key");
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new SettingsError("REMINDERS is not valid JSON");
  }
  if (!isRecord(data)) throw new SettingsError("REMINDERS must map each project to its reminder");
  const entries = Object.entries(data);
  if (entries.length === 0 || entries.length > MAX_REMINDERS) throw new SettingsError(`REMINDERS must hold 1 to ${MAX_REMINDERS} projects`);
  const reminders = entries.map(([project, entry]) => readOne(project, entry));
  if (new Set(reminders.map((r) => r.reminder)).size !== reminders.length) throw new SettingsError("two projects share a reminder check");
  return { reminders, readKey, pingKey };
}

function readOne(project: string, entry: unknown): Reminder {
  if (!SLUG_RE.test(project)) throw new SettingsError("a reminder's project id is not lowercase words joined by hyphens");
  if (!isRecord(entry) || !Object.keys(entry).every((k) => ["name", "watch", "reminder"].includes(k))) {
    throw new SettingsError(`${project}: give only a name, watch and reminder`);
  }
  const { name, watch, reminder } = entry;
  if (typeof name !== "string" || !NAME_RE.test(name)) throw new SettingsError(`${project}: the name is not usable`);
  if (!Array.isArray(watch) || watch.length === 0 || watch.length > MAX_WATCH || !watch.every((w) => typeof w === "string" && SLUG_RE.test(w))) {
    throw new SettingsError(`${project}: watch must be 1 to ${MAX_WATCH} check slugs`);
  }
  if (typeof reminder !== "string" || !SLUG_RE.test(reminder) || (watch as string[]).includes(reminder)) {
    throw new SettingsError(`${project}: the reminder must be its own check slug`);
  }
  return { project, name, watch: watch as string[], reminder };
}

export type CheckStatus = "up" | "down" | "paused" | "other";

// Every check's status by slug, from one read-only call; null if it could not be read.
export async function readStatuses(settings: ReminderSettings, deps: Deps): Promise<Map<string, CheckStatus> | null> {
  try {
    const response = await deps.fetch(API, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
      headers: { "x-api-key": settings.readKey, "user-agent": "yaqzan-checker (read only)" },
    });
    if (response.status !== 200) {
      await response.body?.cancel();
      return null;
    }
    const text = await response.text();
    if (text.length > MAX_ANSWER_BYTES) return null;
    const body = JSON.parse(text) as unknown;
    if (!isRecord(body) || !Array.isArray(body.checks)) return null;
    const out = new Map<string, CheckStatus>();
    for (const c of body.checks as unknown[]) {
      if (isRecord(c) && typeof c.slug === "string" && typeof c.status === "string") out.set(c.slug, kind(c.status));
    }
    return out;
  } catch {
    return null;
  }
}

function kind(status: string): CheckStatus {
  return status === "up" || status === "down" || status === "paused" ? status : "other";
}

// What is remembered per project between runs (in D1).
export interface Remembered {
  readonly downSince: number | null;
  readonly lastSent: number | null;
  readonly lastRefresh: number | null;
}

export type Action = "none" | "remind" | "reset" | "refresh";

export interface Decision {
  readonly action: Action;
  readonly next: Remembered;
  readonly why: string;
}

// One project's decision for this run: pure, so every case is tested without a network.
export function decide(reminder: Reminder, statuses: ReadonlyMap<string, CheckStatus>, before: Remembered, nowMs: number): Decision {
  const down = reminder.watch.filter((slug) => statuses.get(slug) === "down");
  const own = statuses.get(reminder.reminder);
  if (down.length === 0) {
    // Healthy: un-pause or clear the reminder check if needed, and keep it fresh.
    const cleared = { downSince: null, lastSent: null, lastRefresh: nowMs };
    if (own === "paused" || own === "down") {
      return { action: "reset", next: cleared, why: `${reminder.name} is healthy again, so its reminder check is reset for the next outage.` };
    }
    if (before.lastRefresh === null || nowMs - before.lastRefresh >= REFRESH_MS) {
      return { action: "refresh", next: cleared, why: `${reminder.name} is healthy; its reminder check is kept fresh so it never alerts by itself.` };
    }
    return { action: "none", next: { ...cleared, lastRefresh: before.lastRefresh }, why: `${reminder.name} is healthy.` };
  }
  const since = before.downSince ?? nowMs;
  // The first alert came from the check itself, so the first reminder waits a full interval.
  const lastSent = before.lastSent ?? nowMs;
  const remembered = { downSince: since, lastSent, lastRefresh: before.lastRefresh };
  const forHow = minutes(nowMs - since);
  if (own === "paused") {
    return { action: "none", next: remembered, why: `${reminder.name} has been down for ${forHow}; the Chairman acknowledged it, so no reminder is sent.` };
  }
  const every = quiet(nowMs) ? QUIET_EVERY_MS : EVERY_MS;
  if (nowMs - lastSent >= every) {
    return {
      action: "remind",
      next: { ...remembered, lastSent: nowMs },
      why: `${reminder.name} has been down for ${forHow} and nobody has acknowledged it, so the phone is reminded (every ${minutes(every)}${quiet(nowMs) ? " in quiet hours" : ""}).`,
    };
  }
  return { action: "none", next: remembered, why: `${reminder.name} has been down for ${forHow}; the next reminder is due within ${minutes(every - (nowMs - lastSent))}.` };
}

export function quiet(nowMs: number): boolean {
  const hour = new Date(nowMs).getUTCHours();
  return hour >= QUIET_FROM_UTC || hour < QUIET_TO_UTC;
}

function minutes(ms: number): string {
  const total = Math.max(0, Math.round(ms / 60_000));
  if (total < 60) return `${total} min`;
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

// The pings each action needs: "remind" goes up then down, so the down rings again; the up is muted.
export function pingsFor(action: Action, pingKey: string, slug: string): string[] {
  const base = `https://hc-ping.com/${pingKey}/${slug}`;
  if (action === "remind") return [base, `${base}/fail`];
  if (action === "reset" || action === "refresh") return [base];
  return [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const SEND_TIMEOUT_MS = 5_000;

// One reminder run: read every check once, decide per project, ping, remember. Never throws for an
// outside failure: a run that cannot read Healthchecks changes nothing and says so in the log.
export async function runReminders(db: D1Database, settings: ReminderSettings, deps: Deps): Promise<void> {
  const statuses = await readStatuses(settings, deps);
  if (statuses === null) {
    console.log("Reminders: could not read the checks' states from Healthchecks, so no reminder was decided this time; the next run is in 5 minutes.");
    return;
  }
  const remembered = await withRetry("read the reminders' memory", async () => loadRemembered(db), deps.wait);
  for (const reminder of settings.reminders) {
    const before = remembered.get(reminder.project) ?? { downSince: null, lastSent: null, lastRefresh: null };
    const decision = decide(reminder, statuses, before, deps.now());
    let delivered = true;
    for (const url of pingsFor(decision.action, settings.pingKey, reminder.reminder)) {
      delivered = (await ping(deps, url)) && delivered;
    }
    // A reminder that did not reach Healthchecks is not remembered as sent, so the next run tries again.
    const next = delivered ? decision.next : { ...decision.next, lastSent: before.lastSent, lastRefresh: before.lastRefresh };
    await withRetry(`remember ${reminder.name}'s reminder`, async () => saveRemembered(db, reminder.project, next), deps.wait);
    const outcome = decision.action === "none" ? "" : delivered ? " Done." : " Healthchecks did not take it; the next run tries again.";
    console.log(`Reminders: ${decision.why}${outcome}`);
  }
}

async function ping(deps: Deps, url: string): Promise<boolean> {
  try {
    const response = await deps.fetch(url, { method: "POST", redirect: "manual", signal: AbortSignal.timeout(SEND_TIMEOUT_MS) });
    await response.body?.cancel();
    return response.ok;
  } catch {
    return false;
  }
}

export async function loadRemembered(db: D1Database): Promise<Map<string, Remembered>> {
  const { results } = await db
    .prepare("SELECT project, down_since, last_sent, last_refresh FROM reminders")
    .all<{ project: string; down_since: number | null; last_sent: number | null; last_refresh: number | null }>();
  return new Map(results.map((r) => [r.project, { downSince: r.down_since, lastSent: r.last_sent, lastRefresh: r.last_refresh }]));
}

export async function saveRemembered(db: D1Database, project: string, r: Remembered): Promise<void> {
  await db
    .prepare(
      "INSERT INTO reminders (project, down_since, last_sent, last_refresh) VALUES (?, ?, ?, ?) " +
        "ON CONFLICT(project) DO UPDATE SET down_since = excluded.down_since, last_sent = excluded.last_sent, last_refresh = excluded.last_refresh",
    )
    .bind(project, r.downSince, r.lastSent, r.lastRefresh)
    .run();
}
