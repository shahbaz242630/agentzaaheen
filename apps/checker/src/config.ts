// The checker's settings, all held as Worker secrets (never in the repo, Rulebook §7b).
// Everything is checked strictly: a setting that is not exactly right stops the run,
// which makes Healthchecks.io alert (the checker is the thing that broke).

export type Expect = "ok" | "answer";

export interface Target {
  readonly project: string;
  readonly projectName: string;
  readonly url: string;
  readonly expect: Expect;
}

export interface Settings {
  readonly targets: readonly Target[];
  readonly pingKey: string;
  readonly pingUrl: string;
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
const IPV4_RE = /^[0-9.]+$/;
const PING_KEY_RE = /^[A-Za-z0-9_-]{22}$/;
export const PING_URL_RE = /^https:\/\/hc-ping\.com\/[0-9a-f-]{36}$/;

export class SettingsError extends Error {}

export interface RawEnv {
  readonly TARGETS?: string;
  readonly HEALTHCHECKS_PING_KEY?: string;
  readonly HEALTHCHECKS_PING_URL?: string;
}

export function readSettings(env: RawEnv): Settings {
  const pingKey = (env.HEALTHCHECKS_PING_KEY ?? "").trim();
  if (!PING_KEY_RE.test(pingKey)) throw new SettingsError("HEALTHCHECKS_PING_KEY is missing or not a ping key");
  const pingUrl = (env.HEALTHCHECKS_PING_URL ?? "").trim();
  if (!PING_URL_RE.test(pingUrl)) throw new SettingsError("HEALTHCHECKS_PING_URL is missing or not a ping URL");
  return { targets: readTargets(env.TARGETS), pingKey, pingUrl };
}

// TARGETS: {"zaaheen": {"name": "Zaaheen", "sites": [{"url": "https://...", "expect": "ok"}]}}
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
  if (!isRecord(site) || !onlyKeys(site, ["url", "expect"])) {
    throw new SettingsError(`${project}: each site has only a url and an expect`);
  }
  const { url, expect } = site;
  if (typeof url !== "string" || url.length > 300 || !URL_RE.test(url)) {
    throw new SettingsError(`${project}: an address is not an https address by name`);
  }
  if (IPV4_RE.test(new URL(url).hostname)) throw new SettingsError(`${project}: use the address's name, not an IP address`);
  if (expect !== "ok" && expect !== "answer") throw new SettingsError(`${project}: expect must be "ok" or "answer"`);
  return { project, projectName, url, expect };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function onlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}
