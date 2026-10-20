// What the checker remembers in D1: each address's last state (written only on change)
// and hourly counts for the availability SLO. Addresses are known by a hash, never stored.

import type { Change, Seen } from "./alerts";

export const KEEP_HOURS = 90 * 24;
const HOUR_MS = 3_600_000;

export async function addressId(url: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(url));
  return [...new Uint8Array(digest)]
    .slice(0, 8)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function loadState(db: D1Database): Promise<Map<string, boolean>> {
  const { results } = await db.prepare("SELECT id, up FROM state").all<{ id: string; up: number }>();
  return new Map(results.map((row) => [row.id, row.up === 1]));
}

// One batch: a count per address for this hour, and the state of each address that changed.
export async function saveRound(
  db: D1Database,
  seen: readonly Seen[],
  changed: readonly Change[],
  nowMs: number,
  prune: boolean,
): Promise<void> {
  const hour = Math.floor(nowMs / HOUR_MS);
  const statements = [
    ...seen.map((s) =>
      db
        .prepare(
          "INSERT INTO hourly (id, project, hour, total, good) VALUES (?1, ?2, ?3, 1, ?4) " +
            "ON CONFLICT (id, hour) DO UPDATE SET total = total + 1, good = good + excluded.good",
        )
        .bind(s.id, s.target.project, hour, s.up ? 1 : 0),
    ),
    ...changed.map((c) =>
      db
        .prepare(
          "INSERT INTO state (id, project, up, since) VALUES (?1, ?2, ?3, ?4) " +
            "ON CONFLICT (id) DO UPDATE SET up = excluded.up, since = excluded.since",
        )
        .bind(c.id, c.project, c.up ? 1 : 0, nowMs),
    ),
  ];
  // Once an hour the caller asks to forget counts older than 90 days.
  if (prune) {
    statements.push(db.prepare("DELETE FROM hourly WHERE hour < ?1").bind(hour - KEEP_HOURS));
  }
  await db.batch(statements);
}
