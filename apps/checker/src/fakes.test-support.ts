// Test doubles: a pretend internet (status codes by address), a pretend clock and a pretend D1.

import type { Deps } from "./probe";

export interface Sent {
  readonly url: string;
  readonly init: RequestInit;
}

// ``statuses[url]`` is a list consumed one answer per call (the last one repeats); null = no answer.
// Addresses not listed (ntfy, Healthchecks) answer 200 and are recorded in ``sent``.
export function fakeDeps(statuses: Record<string, (number | null)[]>, startMs = Date.UTC(2026, 9, 20, 22, 7, 5)) {
  let clock = startMs;
  const sent: Sent[] = [];
  const calls: string[] = [];
  const waits: number[] = [];
  const deps: Deps = {
    fetch: (url, init) => {
      calls.push(url);
      const queue = statuses[url];
      if (queue === undefined) {
        sent.push({ url, init });
        return Promise.resolve(new Response(null, { status: 200 }));
      }
      const status = queue.length > 1 ? queue.shift() : queue[0];
      if (status === null || status === undefined) return Promise.reject(new TypeError("network error"));
      return Promise.resolve(new Response(null, { status }));
    },
    wait: (ms) => {
      waits.push(ms);
      clock += ms;
      return Promise.resolve();
    },
    now: () => clock,
  };
  return { deps, sent, calls, waits };
}

interface Statement {
  bind: (...values: unknown[]) => Statement;
  all: () => Promise<{ results: { id: string; up: number }[] }>;
  apply: () => void;
}

// Just enough of D1 for the checker's statements, kept in memory.
export class FakeD1 {
  readonly state = new Map<string, { project: string; up: number; since: number }>();
  readonly hourly = new Map<string, { project: string; total: number; good: number }>();
  readonly deleted: number[] = [];
  failBatch = false;

  prepare = (sql: string): Statement => {
    let args: unknown[] = [];
    const statement: Statement = {
      bind: (...values) => {
        args = values;
        return statement;
      },
      all: () => {
        if (!sql.startsWith("SELECT id, up FROM state")) return Promise.reject(new Error(`unexpected query: ${sql}`));
        return Promise.resolve({ results: [...this.state.entries()].map(([id, row]) => ({ id, up: row.up })) });
      },
      apply: () => {
        this.apply(sql, args);
      },
    };
    return statement;
  };

  batch = (statements: Statement[]): Promise<unknown[]> => {
    if (this.failBatch) return Promise.reject(new Error("D1 unavailable"));
    for (const s of statements) s.apply();
    return Promise.resolve([]);
  };

  asD1(): D1Database {
    return this as unknown as D1Database;
  }

  private apply(sql: string, args: unknown[]): void {
    const [a, b, c, d] = args as [string, string, number, number];
    if (sql.startsWith("INSERT INTO hourly")) {
      const key = `${a}@${String(c)}`;
      const row = this.hourly.get(key) ?? { project: b, total: 0, good: 0 };
      this.hourly.set(key, { project: b, total: row.total + 1, good: row.good + d });
    } else if (sql.startsWith("INSERT INTO state")) {
      this.state.set(a, { project: b, up: c, since: d });
    } else if (sql.startsWith("DELETE FROM hourly")) {
      this.deleted.push(args[0] as number);
    } else {
      throw new Error(`unexpected statement: ${sql}`);
    }
  }
}
