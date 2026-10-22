// The checker's settings, all held as Worker secrets (never in the repo, Rulebook §7b).
// Everything is checked strictly: a setting that is not exactly right stops the run,
// which makes Healthchecks.io alert (the checker is the thing that broke).

// "status": up only on exactly the code in `status` (a route only the real service answers that way).
import { SettingsError } from "./errors";
import { guardCalls, readGuards, type GuardSettings, type RawGuardEnv } from "./guard";
import { MAX_REMINDER_CALLS } from "./reminders";

export { SettingsError };

export type Expect = "ok" | "answer" | "status";

export interface Target {
  readonly project: string;
  readonly projectName: string;
  readonly url: string;
  readonly expect: Expect;
  // Our own short name for this part of the product ("api", "sign-in"), never an address.
  readonly part: string | null;
  // Only with expect "status": the one code that counts as up (100 to 499).
  readonly status: number | null;
}

export interface Settings {
  readonly targets: readonly Target[];
  readonly pingKey: string;
  readonly pingUrl: string;
  // Worker guards, checked hourly (null when WORKER_GUARDS is not set).
  readonly guards: GuardSettings | null;
}

// Two rounds of probes plus re-checks must fit Cloudflare's free 50 outbound calls per run:
// at worst 4 per address, 2 per project (one report a round) and the heartbeat.
export const MAX_CALLS = 50;

export function callsNeeded(addresses: number, projects: number): number {
  return 4 * addresses + 2 * projects + 1;
}

const URL_RE = /^https:\/\/[a-z0-9.-]+(\/[A-Za-z0-9._~/-]*)?$/;
const PROJECT_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const NAME_RE = /^[A-Za-z0-9 ]{1,40}$/;
const PART_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const MAX_PART = 24;
const IPV4_RE = /^[0-9.]+$/;
const PING_KEY_RE = /^[A-Za-z0-9_-]{22}$/;
export const PING_URL_RE = /^https:\/\/hc-ping\.com\/[0-9a-f-]{36}$/;


export interface RawEnv extends RawGuardEnv {
  readonly TARGETS?: string;
  readonly HEALTHCHECKS_PING_KEY?: string;
  readonly HEALTHCHECKS_PING_URL?: string;
}

export function readSettings(env: RawEnv): Settings {
  const pingKey = (env.HEALTHCHECKS_PING_KEY ?? "").trim();
  if (!PING_KEY_RE.test(pingKey)) throw new SettingsError("HEALTHCHECKS_PING_KEY is missing or not a ping key");
  const pingUrl = (env.HEALTHCHECKS_PING_URL ?? "").trim();
  if (!PING_URL_RE.test(pingUrl)) throw new SettingsError("HEALTHCHECKS_PING_URL is missing or not a ping URL");
  const targets = readTargets(env.TARGETS);
  const guards = readGuards(env);
  const projects = new Set(targets.map((t) => t.project)).size;
  if (callsNeeded(targets.length, projects) > MAX_CALLS) {
    throw new SettingsError(`TARGETS needs more than the ${MAX_CALLS} calls one run can make`);
  }
  // The guards run in the 5-minute run (index.ts), so they share its budget with the reminders.
  if (guards !== null && MAX_REMINDER_CALLS + guardCalls(guards.projects) > MAX_CALLS) {
    throw new SettingsError(`the reminders and WORKER_GUARDS together need more than the ${MAX_CALLS} calls one run can make`);
  }
  return { targets, pingKey, pingUrl, guards };
}

// TARGETS: {"zaaheen": {"name": "Zaaheen", "sites": [{"url": "https://...", "expect": "ok", "part": "site"}]}}
// ("part" is optional; "status" goes only with expect "status", e.g. {"expect": "status", "status": 405}.)
export function readTargets(raw: string | undefined): Target[] {
  let data: unknown;
  try {
    data = JSON.parse(raw ?? "");
  } catch {
    throw new SettingsError("TARGETS is not valid JSON");
  }
  if (!isRecord(data) || Object.keys(data).length === 0) {
    throw new SettingsError("TARGETS must map each project to its name and sites");
  }
  const targets: Target[] = [];
  for (const [project, entry] of Object.entries(data)) {
    if (!PROJECT_RE.test(project)) throw new SettingsError("a project id is not lowercase words joined by hyphens");
    if (!isRecord(entry) || !onlyKeys(entry, ["name", "sites"])) {
      throw new SettingsError(`${project}: give only a name and sites`);
    }
    const name = entry.name;
    if (typeof name !== "string" || !NAME_RE.test(name)) throw new SettingsError(`${project}: the name is not usable`);
    const sites = entry.sites;
    if (!Array.isArray(sites) || sites.length === 0) throw new SettingsError(`${project}: give a list of sites`);
    for (const site of sites as unknown[]) targets.push(readSite(project, name, site));
  }
  const projects = new Set(targets.map((t) => t.project)).size;
  if (callsNeeded(targets.length, projects) > MAX_CALLS) {
    throw new SettingsError(
      `TARGETS has ${targets.length} sites in ${projects} projects; one run can make at most ${MAX_CALLS} calls`,
    );
  }
  if (new Set(targets.map((t) => t.url)).size !== targets.length) throw new SettingsError("an address is listed twice");
  return targets;
}

function readSite(project: string, projectName: string, site: unknown): Target {
  if (!isRecord(site) || !onlyKeys(site, ["url", "expect", "part", "status"])) {
    throw new SettingsError(`${project}: each site has only a url, an expect, a part and a status`);
  }
  const { url, expect, part, status } = site;
  if (typeof url !== "string" || url.length > 300 || !URL_RE.test(url)) {
    throw new SettingsError(`${project}: an address is not an https address by name`);
  }
  if (IPV4_RE.test(new URL(url).hostname)) throw new SettingsError(`${project}: use the address's name, not an IP address`);
  if (expect !== "ok" && expect !== "answer" && expect !== "status") {
    throw new SettingsError(`${project}: expect must be "ok", "answer" or "status"`);
  }
  if ((expect === "status") !== (status !== undefined)) {
    throw new SettingsError(`${project}: give a status with expect "status", and only then`);
  }
  if (status !== undefined && (typeof status !== "number" || !Number.isInteger(status) || status < 100 || status > 499)) {
    throw new SettingsError(`${project}: a status is a whole number from 100 to 499`);
  }
  if (part !== undefined && (typeof part !== "string" || part.length > MAX_PART || !PART_RE.test(part))) {
    throw new SettingsError(`${project}: a part is not lowercase words joined by hyphens`);
  }
  return { project, projectName, url, expect, part: part ?? null, status: status ?? null };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function onlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}
