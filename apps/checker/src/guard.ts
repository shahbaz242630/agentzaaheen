// Worker guards (ADR-032, R4; incident 2026-10-22): once an hour, check that a product's Worker
// still has every setting it needs and that its latest deploy is its own.
//
// Zaaheen's API Worker was replaced by a website publish that reused its name: the code, all
// its settings and its address went with it, and an address check alone missed it for 15 hours.
// A guard reads the Worker's settings (secret **names** only: Cloudflare sends the values empty
// to a metadata read-only token) and its deploy history, with a token that cannot change anything.
//
// What Cloudflare answers is outside text: names are only compared with our own list, and the
// deploy message is only matched against our own patterns. Neither is ever repeated in a message.

import { SettingsError } from "./errors";
import type { Deps } from "./probe";

export interface Guard {
  readonly worker: string;
  // Our own short name for the part the Worker serves ("api"), as in TARGETS.
  readonly part: string;
  // The secret names it must always have.
  readonly secrets: readonly string[];
  // Starts of deploy messages that mean another service was deployed under this Worker's name.
  readonly notFrom: readonly string[];
}

export interface GuardProject {
  // The Healthchecks check's slug (e.g. "zaaheen-workers"), separate from the address check.
  readonly slug: string;
  readonly name: string;
  readonly guards: readonly Guard[];
}

export interface GuardSettings {
  readonly projects: readonly GuardProject[];
  readonly accountId: string;
  readonly token: string;
}

export type Verdict =
  | { readonly kind: "ok" }
  | { readonly kind: "missing"; readonly missing: readonly string[] }
  | { readonly kind: "wrong_deploy" }
  | { readonly kind: "unread"; readonly status: number | null };

export interface GuardResult {
  readonly guard: Guard;
  readonly verdict: Verdict;
}

export interface GuardReport {
  readonly slug: string;
  readonly down: boolean;
  readonly message: string;
}

// Two reads per guarded Worker, and one report per project.
export function guardCalls(projects: readonly GuardProject[]): number {
  return projects.reduce((sum, p) => sum + 2 * p.guards.length + 1, 0);
}

const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const NAME_RE = /^[A-Za-z0-9 ]{1,40}$/;
const WORKER_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const PART_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const SECRET_RE = /^[A-Z][A-Z0-9_]{0,63}$/;
const NOT_FROM_RE = /^[A-Za-z0-9][A-Za-z0-9 .:/_-]{0,59}$/;
const ACCOUNT_RE = /^[0-9a-f]{32}$/;
const TOKEN_RE = /^[A-Za-z0-9_-]{30,80}$/;
const MAX_SECRETS = 50;
const MAX_NOT_FROM = 5;
const MAX_GUARDS = 5;

export interface RawGuardEnv {
  readonly WORKER_GUARDS?: string;
  readonly CLOUDFLARE_ACCOUNT_ID?: string;
  readonly CLOUDFLARE_READ_TOKEN?: string;
}

// Guards are optional: no WORKER_GUARDS means none. When it is set, the account and token must be too.
// WORKER_GUARDS: {"zaaheen-workers": {"name": "Zaaheen", "guards": [{"worker": "zaaheen-account",
//   "part": "api", "secrets": ["CLERK_SECRET_KEY", ...], "not_from": ["account.zaaheen.com from"]}]}}
export function readGuards(env: RawGuardEnv): GuardSettings | null {
  const raw = (env.WORKER_GUARDS ?? "").trim();
  if (raw === "") return null;
  const accountId = (env.CLOUDFLARE_ACCOUNT_ID ?? "").trim();
  if (!ACCOUNT_RE.test(accountId)) throw new SettingsError("CLOUDFLARE_ACCOUNT_ID is missing or not an account id");
  const token = (env.CLOUDFLARE_READ_TOKEN ?? "").trim();
  if (!TOKEN_RE.test(token)) throw new SettingsError("CLOUDFLARE_READ_TOKEN is missing or not a token");
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new SettingsError("WORKER_GUARDS is not valid JSON");
  }
  if (!isRecord(data) || Object.keys(data).length === 0) throw new SettingsError("WORKER_GUARDS must map each check to its name and guards");
  const projects: GuardProject[] = [];
  for (const [slug, entry] of Object.entries(data)) {
    if (!SLUG_RE.test(slug)) throw new SettingsError("a guard check's slug is not lowercase words joined by hyphens");
    if (!isRecord(entry) || !onlyKeys(entry, ["name", "guards"])) throw new SettingsError(`${slug}: give only a name and guards`);
    if (typeof entry.name !== "string" || !NAME_RE.test(entry.name)) throw new SettingsError(`${slug}: the name is not usable`);
    const guards = entry.guards;
    if (!Array.isArray(guards) || guards.length === 0 || guards.length > MAX_GUARDS) {
      throw new SettingsError(`${slug}: give a list of 1 to ${MAX_GUARDS} guards`);
    }
    projects.push({ slug, name: entry.name, guards: (guards as unknown[]).map((g) => readGuard(slug, g)) });
  }
  return { projects, accountId, token };
}

function readGuard(slug: string, raw: unknown): Guard {
  if (!isRecord(raw) || !onlyKeys(raw, ["worker", "part", "secrets", "not_from"])) {
    throw new SettingsError(`${slug}: each guard has only a worker, a part, secrets and not_from`);
  }
  const { worker, part, secrets, not_from: notFrom } = raw;
  if (typeof worker !== "string" || !WORKER_RE.test(worker)) throw new SettingsError(`${slug}: a worker name is not usable`);
  if (typeof part !== "string" || part.length > 24 || !PART_RE.test(part)) throw new SettingsError(`${slug}: a part is not lowercase words joined by hyphens`);
  if (!stringList(secrets, SECRET_RE, MAX_SECRETS) || secrets.length === 0) {
    throw new SettingsError(`${slug}: secrets must be 1 to ${MAX_SECRETS} names in capitals`);
  }
  if (!stringList(notFrom, NOT_FROM_RE, MAX_NOT_FROM)) throw new SettingsError(`${slug}: not_from must be up to ${MAX_NOT_FROM} short message starts`);
  return { worker, part, secrets: [...new Set(secrets)].sort(), notFrom };
}

const API = "https://api.cloudflare.com/client/v4/accounts";
const READ_TIMEOUT_MS = 8_000;
const MAX_ANSWER_BYTES = 256 * 1024;

// One guard: its settings, then its deploy history. Any failure to read is "unread", never "ok".
export async function checkGuard(guard: Guard, settings: GuardSettings, deps: Deps): Promise<GuardResult> {
  const base = `${API}/${settings.accountId}/workers/scripts/${guard.worker}`;
  const config = await readJson(`${base}/settings`, settings.token, deps);
  if (!config.ok) return { guard, verdict: { kind: "unread", status: config.status } };
  const names = secretNames(config.body);
  if (names === null) return { guard, verdict: { kind: "unread", status: config.status } };
  const missing = guard.secrets.filter((s) => !names.has(s));
  if (missing.length > 0) return { guard, verdict: { kind: "missing", missing } };
  const history = await readJson(`${base}/deployments`, settings.token, deps);
  if (!history.ok) return { guard, verdict: { kind: "unread", status: history.status } };
  const message = latestMessage(history.body);
  if (message === undefined) return { guard, verdict: { kind: "unread", status: history.status } };
  if (message !== null && guard.notFrom.some((start) => message.startsWith(start))) return { guard, verdict: { kind: "wrong_deploy" } };
  return { guard, verdict: { kind: "ok" } };
}

type Read = { readonly ok: true; readonly status: number; readonly body: unknown } | { readonly ok: false; readonly status: number | null };

async function readJson(url: string, token: string, deps: Deps): Promise<Read> {
  try {
    const response = await deps.fetch(url, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
      headers: { authorization: `Bearer ${token}`, "user-agent": "yaqzan-checker (read only)" },
    });
    if (response.status !== 200) {
      await response.body?.cancel();
      return { ok: false, status: response.status };
    }
    const text = await response.text();
    if (text.length > MAX_ANSWER_BYTES) return { ok: false, status: response.status };
    return { ok: true, status: response.status, body: JSON.parse(text) as unknown };
  } catch {
    return { ok: false, status: null };
  }
}

// The names of the Worker's secret bindings, or null if the answer is not the expected shape.
function secretNames(body: unknown): Set<string> | null {
  if (!isRecord(body) || body.success !== true || !isRecord(body.result) || !Array.isArray(body.result.bindings)) return null;
  const names = new Set<string>();
  for (const b of body.result.bindings as unknown[]) {
    if (isRecord(b) && b.type === "secret_text" && typeof b.name === "string") names.add(b.name);
  }
  return names;
}

// The latest deploy's message (null when it has none), or undefined if the answer is not the expected shape.
function latestMessage(body: unknown): string | null | undefined {
  if (!isRecord(body) || body.success !== true || !isRecord(body.result) || !Array.isArray(body.result.deployments)) return undefined;
  const deployments = body.result.deployments as unknown[];
  if (deployments.length === 0 || !deployments.every((d) => isRecord(d) && typeof d.created_on === "string")) return undefined;
  // The newest by its own date, whatever order the list comes in (ISO dates sort as text).
  const latest = (deployments as Record<string, unknown>[]).reduce((a, b) => ((b.created_on as string) > (a.created_on as string) ? b : a));
  const notes = latest.annotations;
  const message = isRecord(notes) ? notes["workers/message"] : undefined;
  return typeof message === "string" ? message : null;
}

// One plain-English report per project: what is wrong, why it matters, and what to check.
// Only our own words, our own part names and names from our own list.
export function guardReport(project: GuardProject, results: readonly GuardResult[]): GuardReport {
  const lines = results.map((r) => describe(project.name, r));
  const down = results.some((r) => r.verdict.kind !== "ok");
  return { slug: project.slug, down, message: lines.join("\n") };
}

function describe(name: string, { guard, verdict }: GuardResult): string {
  const who = `${name} ${guard.part}`;
  const total = guard.secrets.length;
  switch (verdict.kind) {
    case "ok":
      return `${who}: all ${total} settings present and its latest deploy is its own.`;
    case "missing":
      return (
        `${who} is missing ${verdict.missing.length} of its ${total} settings (${verdict.missing.join(", ")}), ` +
        "so whatever uses them will fail. Most likely a deploy replaced the Worker or a setting was deleted: " +
        "check its deploy history, then re-enter the missing settings."
      );
    case "wrong_deploy":
      return (
        `${who}: its latest deploy is another service's publish, not its own, so it has probably been replaced. ` +
        "Check which Worker owns its address and redeploy it from its own config."
      );
    case "unread": {
      const why = verdict.status === null ? "no answer" : `http ${verdict.status}`;
      return (
        `${who}: could not read its settings from Cloudflare (${why}), so its health is unknown, not fine. ` +
        "The read-only token may have expired or lost access."
      );
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function onlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

function stringList(value: unknown, re: RegExp, max: number): value is string[] {
  return Array.isArray(value) && value.length <= max && value.every((v) => typeof v === "string" && re.test(v));
}
