// One status-only check of one address, judged by the same rules as the laptop probe
// (core/sdk/uptime.py): the page is never read, redirects are never followed.

import type { Expect, Target } from "./config";

export type Reason = "ok" | "redirect" | "answered" | "server_error" | "not_found" | "refused" | "no_answer";

// Our own words for each failure, the same as core/notify (never text a website wrote).
export const REASON_WORDS: Readonly<Record<Reason, string>> = {
  ok: "up",
  redirect: "up",
  answered: "up",
  server_error: "server error",
  not_found: "page not found",
  refused: "refused the request",
  no_answer: "did not answer",
};

export const TIMEOUT_MS = 8_000;
export const RECHECK_AFTER_MS = 10_000;

export interface Judged {
  readonly up: boolean;
  readonly reason: Reason;
}

export interface Checked extends Judged {
  readonly target: Target;
}

export interface Deps {
  readonly fetch: (url: string, init: RequestInit) => Promise<Response>;
  readonly wait: (ms: number) => Promise<void>;
  readonly now: () => number;
}

export function judge(expect: Expect, status: number | null): Judged {
  if (status === null) return { up: false, reason: "no_answer" };
  if (status >= 500) return { up: false, reason: "server_error" };
  if (status >= 200 && status < 300) return { up: true, reason: "ok" };
  if (status >= 300 && status < 400) return { up: true, reason: "redirect" };
  if (expect === "answer") return { up: true, reason: "answered" };
  return { up: false, reason: status === 404 ? "not_found" : "refused" };
}

// The status code, or null if the address did not answer in time.
export async function statusOf(url: string, deps: Deps): Promise<number | null> {
  try {
    const response = await deps.fetch(url, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
      headers: { "user-agent": "yaqzan-checker (status only)" },
    });
    await response.body?.cancel();
    return response.status;
  } catch {
    return null;
  }
}

// Every address once; any that fails is checked again after a short wait, so a single
// blip never alerts. An address is down only if both checks fail.
export async function checkAll(targets: readonly Target[], deps: Deps): Promise<Checked[]> {
  const first = await Promise.all(targets.map(async (target) => check(target, deps)));
  if (first.every((c) => c.up)) return first;
  await deps.wait(RECHECK_AFTER_MS);
  return Promise.all(first.map(async (c) => (c.up ? c : check(c.target, deps))));
}

async function check(target: Target, deps: Deps): Promise<Checked> {
  return { target, ...judge(target.expect, await statusOf(target.url, deps)) };
}
